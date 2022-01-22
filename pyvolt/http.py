from __future__ import annotations

from typing import (TYPE_CHECKING, Any, Coroutine, Dict, List, Iterable, Literal, Optional, 
                    TypeVar, ClassVar, Type, Union, overload)

import asyncio
import sys

from urllib.parse import quote as _uriquote
import weakref

import aiohttp

try:
    import ujson as _json
except ImportError:
    import json as _json

from .errors import HTTPException, Forbidden, NotFound, RevoltServerError, LoginFailure
from . import __version__
from .utils import MISSING

if TYPE_CHECKING:
    from .token import Token
    from .file import File
    from .enums import SortType
    
    from .types import (
        http,
        embed,
        message,
        user
    )
    from .types.snowflake import Snowflake, SnowflakeList
    
    from types import TracebackType
    
    T = TypeVar('T')
    BE = TypeVar('BE', bound=BaseException)
    MU = TypeVar('MU', bound='MaybeUnlock')
    Response = Coroutine[Any, Any, T]
    

async def json_or_text(response: aiohttp.ClientResponse) -> Union[Dict[str, Any], str]:
    text = await response.text(encoding='utf-8')
    try:
        if response.headers['content-type'] == 'application/json':
            return _json.loads(text)
    except KeyError:
        # Thanks Cloudflare
        pass

    return text


class Route:
    BASE: ClassVar[str] = 'https://api.revolt.chat'

    def __init__(self, method: str, path: str, **parameters: Any) -> None:
        self.path: str = path
        self.method: str = method
        
        url = self.BASE + self.path
        if parameters:
            url = url.format_map({k: _uriquote(v) if isinstance(v, str) else v for k, v in parameters.items()})
        self.url: str = url

        # major parameters:
        self.channel_id: Optional[Snowflake] = parameters.get('channel_id')
        self.server_id: Optional[Snowflake] = parameters.get('server_id')

    @property
    def bucket(self) -> str:
        # the bucket is just method + path w/ major parameters
        return f'{self.channel_id}:{self.server_id}:{self.path}'
    

class MaybeUnlock:
    def __init__(self, lock: asyncio.Lock) -> None:
        self.lock: asyncio.Lock = lock
        self._unlock: bool = True

    def __enter__(self: MU) -> MU:
        return self

    def defer(self) -> None:
        self._unlock = False

    def __exit__(
        self,
        exc_type: Optional[Type[BE]],
        exc: Optional[BE],
        traceback: Optional[TracebackType],
    ) -> None:
        if self._unlock:
            self.lock.release()
            

class HTTPClient:
    """Represents an HTTP client sending HTTP requests to the Revolt API."""
    
    def __init__(
        self, 
        connector: Optional[aiohttp.BaseConnector] = None, 
        *, loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self.loop: asyncio.AbstractEventLoop = asyncio.get_event_loop() if loop is None else loop
        self.connector = connector
        self.__session: aiohttp.ClientSession = MISSING
        self._locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
        self._global_over: asyncio.Event = asyncio.Event()
        self._global_over.set()
        # values set in static login
        self.token: Optional[Token] = None 
        self.api_info: Optional[http.ApiInfo] = None 
        
        user_agent = 'Pyvolt (https://github.com/Gael-devv/Pyvolt {0}) Python/{1[0]}.{1[1]} aiohttp/{2}'
        self.user_agent: str = user_agent.format(__version__, sys.version_info, aiohttp.__version__)
        
    def recreate(self) -> None:
        if self.__session.closed:
            self.__session = aiohttp.ClientSession(
                connector=self.connector, # ws_response_class=in the uture
            )
        
    async def request(
        self,
        route: Route,
        *,
        form: Optional[Iterable[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> Any:
        bucket = route.bucket
        method = route.method
        url = route.url

        lock = self._locks.get(bucket)
        if lock is None:
            lock = asyncio.Lock()
            if bucket is not None:
                self._locks[bucket] = lock
        
        # header creation
        headers: Dict[str, str] = {
            'User-Agent': self.user_agent,
        }

        # authorization in Revolt API
        if self.token is not None:
            headers[f'x-{self.token.type}-token'] = self.token.value
        
        # some checking if it's a JSON request
        if 'json' in kwargs:
            headers['Content-Type'] = 'application/json'
            kwargs["data"] = _json.dumps(kwargs.pop('json'))

        kwargs['headers'] = headers
        
        if not self._global_over.is_set():
            # wait until the global lock is complete
            await self._global_over.wait()
        
        response: Optional[aiohttp.ClientResponse] = None
        data: Optional[Union[Dict[str, Any], str]] = None
        await lock.acquire()
        with MaybeUnlock(lock) as maybe_lock:
            for tries in range(5):
                if form:
                    form_data = aiohttp.FormData()
                    for params in form:
                        form_data.add_field(**params)
                    kwargs['data'] = form_data

                try:
                    async with self.__session.request(method, url, **kwargs) as response:
                        # even errors have text involved in them so this is safe to call
                        data = await json_or_text(response)

                        # the request was successful so just return the text/json
                        if 300 > response.status >= 200:
                            return data

                        # we are being rate limited
                        if response.status == 429:
                            if not response.headers.get('Via') or isinstance(data, str):
                                # Banned by Cloudflare more than likely.
                                raise HTTPException(response, data)

                            # sleep a bit
                            retry_after: float = data['retry_after']

                            # check if it's a global rate limit
                            is_global = data.get('global', False)
                            if is_global:
                                self._global_over.clear()

                            await asyncio.sleep(retry_after)

                            # release the global lock now that the
                            # global rate limit has passed
                            if is_global:
                                self._global_over.set()

                            continue

                        # we've received a 500, 502, or 504, unconditional retry
                        if response.status in {500, 502, 504}:
                            await asyncio.sleep(1 + tries * 2)
                            continue

                        # the usual error cases
                        if response.status == 403:
                            raise Forbidden(response, data)
                        elif response.status == 404:
                            raise NotFound(response, data)
                        elif response.status >= 500:
                            raise RevoltServerError(response, data)
                        else:
                            raise HTTPException(response, data)

                # This is handling exceptions from the request
                except OSError as e:
                    # Connection reset by peer
                    if tries < 4 and e.errno in (54, 10054):
                        await asyncio.sleep(1 + tries * 2)
                        continue
                    raise

            if response is not None:
                # We've run out of retries, raise.
                if response.status >= 500:
                    raise RevoltServerError(response, data)

                raise HTTPException(response, data)

            raise RuntimeError('Unreachable code in HTTP handling')
    
    async def upload_file(self, file: File, tag: str) -> http.Autumn:
        url = f"{self.api_info['features']['autumn']['url']}/{tag}"

        headers = {
            "User-Agent": self.user_agent
        }

        form = aiohttp.FormData()
        form.add_field("file", file.fp.read(), filename=file.filename)

        async with self.__session.post(url, data=form, headers=headers) as response:
            data: http.Autumn = await json_or_text(response)
        
        if response.status == 400:
            raise HTTPException(response, data)
        elif 500 <= response.status <= 600:
            raise RevoltServerError(response, data)
        else:
            return data
    
    # state management
    
    async def close(self) -> None:
        if self.__session:
            await self.__session.close()
    
    # login management
    
    async def static_login(self, token: Token) -> user.User:
        # Necessary to get aiohttp to stop complaining about session creation
        self.__session = aiohttp.ClientSession(connector=self.connector)
        self.api_info = await self.get_api_info()
        old_token = self.token
        self.token = token

        try: 
            data = await self.request(Route('GET', '/users/@me'))
        except HTTPException as exc:
            self.token = old_token
            if exc.status == 401:
                raise LoginFailure('Improper token has been passed.') from exc
            raise

        return data
    
    # core management
    
    async def get_api_info(self) -> http.ApiInfo:
        return await self.request(Route('GET', '/'))
    
    # Message management
    
    async def send_message(
        self, 
        channel_id: Snowflake, 
        content: Optional[str], 
        *,
        embed: Optional[embed.TextEmbed] = None,
        embeds: Optional[List[embed.TextEmbed]] = None,
        attachment: Optional[File] = None,
        attachments: Optional[List[File]] = None, 
        replie: Optional[List[message.MessageReply]] = None, 
        replies: Optional[List[message.MessageReply]] = None, 
        masquerade: Optional[message.Masquerade] = None
    ) -> message.Message:
        r = Route("POST", "/channels/{channel_id}/messages", channel_id=channel_id)
        payload: dict[str, Any] = {}

        if content:
            payload["content"] = content

        if embed:
            payload["embeds"] = [embed]

        if embeds:
            payload["embeds"] = embeds

        if attachment:
            data = await self.upload_file(attachment, "attachments")
            payload["attachments"] = [data["id"]]

        if attachments:
            attachment_ids: list[str] = []

            for _attachment in attachments:
                data = await self.upload_file(_attachment, "attachments")
                attachment_ids.append(data["id"])

            payload["attachments"] = attachment_ids

        if replie:
            payload["replies"] = [replie]

        if replies:
            payload["replies"] = replies

        if masquerade:
            payload["masquerade"] = masquerade

        return await self.request(r, json=payload)

    def edit_message(
        self, 
        channel_id: Snowflake, 
        message_id: Snowflake, 
        content: Optional[str],
        *,
        embed: Optional[embed.TextEmbed] = None,
        embeds: Optional[List[embed.TextEmbed]] = None,
    ) -> Response[None]:
        r = Route("PATCH", "/channels/{channel_id}/messages/{message_id}", channel_id=channel_id, message_id=message_id)
        payload: dict[str, Any] = {}

        if content:
            payload["content"] = content

        if embed:
            payload["embeds"] = [embed]

        if embeds:
            payload["embeds"] = embeds
        
        return self.request(r, json=payload)
    
    def delete_message(self, channel_id: Snowflake, message_id: Snowflake) -> Response[None]:
        r = Route("DELETE", "/channels/{channel_id}/messages/{message_id}", channel_id=channel_id, message_id=message_id)
        return self.request(r)
    
    def fetch_message(self, channel_id: str, message_id: str) -> Response[message.Message]:
        r = Route("GET", "/channels/{channel_id}/messages/{message_id}", channel_id=channel_id, message_id=message_id)
        return self.request(r)
    
    def fetch_messages(
        self, 
        channel_id: Snowflake, 
        sort: SortType,
        *, 
        limit: Optional[int] = None, 
        before: Optional[str] = None, 
        after: Optional[str] = None, 
        nearby: Optional[str] = None, 
        include_users: bool = False
    ) -> Response[Union[List[message.Message], http.MessageWithUserData]]:
        r = Route("GET", "/channels/{channel_id}/messages", channel_id=channel_id)
        payload: dict[str, Any] = {"sort": sort.value, "include_users": str(include_users)}

        if limit:
            payload["limit"] = limit

        if before:
            payload["before"] = before

        if after:
            payload["after"] = after

        if nearby:
            payload["nearby"] = nearby

        return self.request(r, json=payload)
    
    def search_messages(
        self, 
        channel_id: Snowflake, 
        query: str,
        *, 
        limit: Optional[int] = None, 
        before: Optional[str] = None, 
        after: Optional[str] = None,
        sort: Optional[SortType] = None,
        include_users: bool = False
    ) -> Response[Union[List[message.Message], http.MessageWithUserData]]:
        r = Route("POST", "/channels/{channel_id}/search", channel_id=channel_id)
        payload = {"query": query, "include_users": include_users}

        if limit:
            payload["limit"] = limit

        if before:
            payload["before"] = before

        if after:
            payload["after"] = after

        if sort:
            payload["sort"] = sort.value

        return self.request(r, json=payload)
    
    def poll_message_changes(self, channel_id: Snowflake, message_ids: SnowflakeList): 
        r = Route("POST", "/channels/{channel_id}/messages/stale", channel_id=channel_id)
        payload = {"ids": message_ids}
        
        return self.request(r, json=payload)
    
    def ack_message(self, channel_id: Snowflake, message_id: Snowflake): 
        r = Route("PUT", "/channels/{channel_id}/ack/{message_id}", channel_id=channel_id, message_id=message_id)
        return self.request(r)
    