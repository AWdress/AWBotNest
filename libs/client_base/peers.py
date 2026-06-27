# 标准库
import asyncio
from typing import Set, List, Union

# 第三方库
from core.telegram import PeerIdInvalid

# 自定义模块
from libs.log import logger

class PeerManagerMixin:
    """
    处理 Telegram Peer (用户/群组/频道) 的管理逻辑，包括黑名单和有效性验证。
    """
    def __init_peer_manager__(self):
        self._invalid_peer_ids: Set[int] = set()  # 无效聊天ID黑名单
        self._max_invalid_peers: int = 100  # 最大黑名单数量
        self._valid_group_ids: Set[int] = set()  # 有效群组ID列表

    def _is_peer_invalid(self, peer_id: int) -> bool:
        return peer_id in self._invalid_peer_ids
    
    def _add_invalid_peer(self, peer_id: int):
        if len(self._invalid_peer_ids) >= self._max_invalid_peers:
            invalid_list = list(self._invalid_peer_ids)
            self._invalid_peer_ids = set(invalid_list[self._max_invalid_peers // 2:])
        
        self._invalid_peer_ids.add(peer_id)
        logger.debug(f"已将聊天ID {peer_id} 添加到黑名单，当前黑名单大小: {len(self._invalid_peer_ids)}")
    
    def _remove_invalid_peer(self, peer_id: int):
        if peer_id in self._invalid_peer_ids:
            self._invalid_peer_ids.remove(peer_id)
            logger.info(f"已将聊天ID {peer_id} 从无效列表中移除")
    
    def set_valid_group_ids(self, group_ids: List[int]):
        self._valid_group_ids = set(group_ids)
        logger.info(f"已设置 {len(self._valid_group_ids)} 个有效群组ID")
    
    def is_valid_group_id(self, peer_id: int) -> bool:
        return peer_id in self._valid_group_ids

    async def _validate_peer_id(self, peer_id: int) -> bool:
        try:
            if self._is_peer_invalid(peer_id):
                return False
            chat = await self.get_chat(peer_id)
            if chat:
                self._remove_invalid_peer(peer_id)
                return True
            return False
        except PeerIdInvalid:
            self._add_invalid_peer(peer_id)
            return False
        except Exception as e:
            logger.warning(f"验证聊天ID时出错: {e}")
            return False
