from __future__ import annotations

from typing import TYPE_CHECKING, List, TypedDict, Union

if TYPE_CHECKING:
    from .file import File
    from .embed import EmbedType
    from .snowflake import Snowflake

__all__ = (
    "Message",
    "MessageReplyPayload",
    "Masquerade"
)


class UserAddContent(TypedDict):
    id: str
    by: str


class UserRemoveContent(TypedDict):
    id: str
    by: str


class UserJoinedContent(TypedDict):
    id: str
    by: str


class UserLeftContent(TypedDict):
    id: str


class UserKickedContent(TypedDict):
    id: str


class UserBannedContent(TypedDict):
    id: str


class ChannelRenameContent(TypedDict):
    name: str
    by: str


class ChannelIconChangeContent(TypedDict):
    by: str


class ChannelDescriptionChangeContent(TypedDict):
    by: str


MessageEdited = TypedDict("MessageEdited", {"$date": str})


class Masquerade(TypedDict, total=False):
    name: str
    avatar: str


class _OptionalMessage(TypedDict):
    attachments: List[File]
    embeds: List[EmbedType]
    mentions: List[str]
    replies: List[str]
    edited: MessageEdited
    masquerade: Masquerade


class Message(_OptionalMessage):
    _id: Snowflake
    channel: str
    author: str
    content: Union[
        str, 
        UserAddContent, 
        UserRemoveContent, 
        UserJoinedContent, 
        UserLeftContent, 
        UserKickedContent, 
        UserBannedContent, 
        ChannelRenameContent, 
        ChannelIconChangeContent,
        ChannelDescriptionChangeContent, 
    ]


class MessageReply(TypedDict):
    id: str
    mention: bool