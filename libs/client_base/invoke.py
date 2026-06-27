# 标准库
import asyncio
import re
import traceback

# 第三方库
from core.telegram import (
    RPCError,
    FloodWait,
    PeerIdInvalid,
)

# 自定义模块
from libs.log import logger

class InvokeMixin:
    """
    重写 Pyrogram 的调用逻辑，增加重试、流量控制及自动黑名单过滤。
    """
    async def _custom_invoke(self, query, *args, **kwargs):
        # 1. 预检查：如果 Peer 在黑名单中则直接跳过
        try:
            peer_id = self._extract_peer_id(query)
            if peer_id and self._is_peer_invalid(peer_id):
                return None
        except Exception:
            pass
        
        retries = 0
        while retries < self._invoke_retries:
            async with self._pool_semaphore:
                try:
                    return await self._session_invoke(query, *args, **kwargs)
                
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                    retries += 1

                except asyncio.TimeoutError:
                    await asyncio.sleep(1)
                    retries += 1
                    if retries >= self._invoke_retries:
                        logger.error(f"TimeoutError for {query.__class__.__name__}")

                except RPCError as e:
                    if await self._handle_rpc_error(query, e):
                        return None # 错误已处理（如已拉黑），不再重试
                    
                    await asyncio.sleep(1)
                    retries += 1

                except PeerIdInvalid as e:
                    peer_id = self._extract_id_from_exception(e)
                    if peer_id:
                        self._add_invalid_peer(peer_id)
                        logger.info(f"Peer ID {peer_id} 无效，已加入黑名单")
                    return None

                except Exception as e:
                    if "database" in str(e).lower():
                        return None
                    await asyncio.sleep(1)
                    retries += 1
        
        return None

    def _extract_peer_id(self, query):
        if hasattr(query, 'peer'):
            if hasattr(query.peer, 'channel_id'): return -1000000000000 - query.peer.channel_id
            if hasattr(query.peer, 'user_id'): return query.peer.user_id
            if hasattr(query.peer, 'chat_id'): return -query.peer.chat_id
        return None

    def _extract_id_from_exception(self, e):
        try:
            tb_str = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
            match = re.search(r'ID not found: (-?\d+)', tb_str)
            return int(match.group(1)) if match else None
        except Exception:
            return None

    async def _handle_rpc_error(self, query, e) -> bool:
        """返回 True 表示错误已处理且无需重试"""
        msg = str(e)
        extracted_id = self._extract_id_from_exception(e)
        
        if "CHANNEL_INVALID" in msg or "CHANNEL_PRIVATE" in msg:
            if extracted_id: self._add_invalid_peer(extracted_id)
            return True
        
        if any(x in msg for x in ["STICKERSET_INVALID", "MESSAGE_IDS_EMPTY", "MESSAGE_NOT_FOUND", "MESSAGE_ID_INVALID"]):
            return True
            
        return False
