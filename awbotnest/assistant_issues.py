"""Bounded local issue drafts and explicitly approved GitHub submissions.

There is intentionally no generic HTTP tool here. The administrator assistant
owns human confirmation; this service owns the final policy/snapshot check and
the single-use delivery record. An unknown result is never retried.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from .logs import redact_secrets

MAX_DRAFTS = 20
MAX_TITLE = 180
MAX_BODY = 12000
MAX_PAYLOAD_BYTES = 24000
APPROVAL_SECONDS = 15 * 60
_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}\Z")
_ID = re.compile(r"[a-f0-9]{32}\Z")
_BARE_SECRET = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}|cb_[A-Za-z0-9_-]{12,}|"
    r"\d{6,}:[A-Za-z0-9_-]{20,})\b"
)
_UNCERTAIN = "提交结果未确认，请先到 GitHub 检查；系统不会重复提交"


def normalize_repository(value):
    if (not isinstance(value, str) or not _REPO.fullmatch(value)
            or value.rsplit("/", 1)[1] in {".", ".."}):
        raise ValueError("仓库格式应为 owner/repo")
    return value.lower()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


class AssistantIssues:
    def __init__(self, deps, store, *, settings, clean):
        self.deps, self.store = deps, store
        self.settings, self.clean = settings, clean
        self._lock = asyncio.Lock()
        self._tickets = {}

    async def recover(self):
        """A restart cannot distinguish lost acknowledgements from lost requests."""
        async with self._lock:
            return await self.store.interrupt_issue_submissions(_UNCERTAIN)

    def _sanitize(self, text, limit):
        value = self.clean(text, limit=limit * 4)
        value = redact_secrets(_BARE_SECRET.sub("********", str(value)))
        # Images, raw HTML and mentions must not silently notify people or load
        # attachments. The exact resulting text is shown before confirmation.
        value = re.sub(r"!\[[^\]\r\n]*\](?:\([^\r\n]*?\)|\[[^\]\r\n]*\])",
                       "[图片未附带]", value)
        value = value.replace("![", "[图片未附带：")  # Shortcut/reference-style images too.
        value = value.replace("<", "&lt;").replace(">", "&gt;")
        value = re.sub(r"(?<![\w@])@(?=[A-Za-z0-9])", "@\u200b", value)
        value = "".join(ch for ch in value if ord(ch) >= 32 or ch in "\n\t")
        if len(value) > limit:
            raise ValueError("脱敏后的反馈内容过长，请缩短后重新生成")
        return value

    def _content(self, title, body):
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE:
            raise ValueError(f"标题不能为空，且不超过 {MAX_TITLE} 字")
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY:
            raise ValueError(f"正文不能为空，且不超过 {MAX_BODY} 字")
        if any(ch in title for ch in "\r\n\x00"):
            raise ValueError("标题不能包含换行")
        title, body = self._sanitize(title.strip(), MAX_TITLE), self._sanitize(body.strip(), MAX_BODY)
        if not title.strip() or not body.strip():
            raise ValueError("反馈内容不能为空")
        if len(json.dumps({"title": title, "body": body}, ensure_ascii=False).encode()) > MAX_PAYLOAD_BYTES:
            raise ValueError("反馈内容过长，请缩短后重新生成")
        return title, body

    def _policy(self, repository):
        config = self.settings()
        if not isinstance(config, dict) or config.get("enabled") is not True:
            raise PermissionError("系统助手已关闭")
        if config.get("allow_issue_submission") is not True:
            raise PermissionError("尚未允许提交 GitHub Issue；本地草稿仍可保留")
        if config.get("allow_actions") is not True:
            raise PermissionError("尚未允许助手执行写入操作")
        allowed = config.get("issue_repositories")
        if not isinstance(allowed, list) or len(allowed) > 20:
            raise PermissionError("Issue 仓库授权配置不正确")
        try:
            normalized = [normalize_repository(repo) for repo in allowed]
        except ValueError:
            raise PermissionError("Issue 仓库授权配置不正确") from None
        if repository not in normalized:
            raise PermissionError("此仓库尚未获得 Issue 提交授权")
        token = getattr(self.deps.settings, "github_token", "")
        if (not isinstance(token, str) or not token.strip() or len(token) > 4096
                or any(ord(ch) < 33 or ord(ch) > 126 for ch in token)):
            raise PermissionError("请管理员先在系统设置填写有 Issue 写入权限的 GitHub Token")
        # Only a one-way digest is ever kept in a draft or preparation ticket.
        policy = {"enabled": True, "allow_issue_submission": True,
                  "allow_actions": True,
                  "issue_repositories": sorted(set(normalized)), "token": token,
                  "proxy": getattr(self.deps.settings, "proxy_url", "")}
        return token, _digest(policy)

    def status(self):
        config = self.settings()
        if not isinstance(config, dict):
            config = {}
        repositories = config.get("issue_repositories", [])
        try:
            repositories = list(dict.fromkeys(normalize_repository(repo) for repo in repositories)) \
                if isinstance(repositories, list) and len(repositories) <= 20 else []
        except ValueError:
            repositories = []
        token = getattr(self.deps.settings, "github_token", "")
        return {"configured": bool(isinstance(token, str) and token.strip()),
                "enabled": (config.get("enabled") is True and config.get("allow_actions") is True
                            and config.get("allow_issue_submission") is True),
                "repositories": repositories}

    @staticmethod
    def _arguments(draft):
        return {"draft_id": draft["id"], "repository": draft["repository"],
                "title": draft["title"], "body": draft["body"]}

    @staticmethod
    def _public(draft):
        return {key: copy.deepcopy(value) for key, value in draft.items()
                if key not in {"content_digest", "submission_fingerprint"}}

    async def list_drafts(self):
        return [self._public(item) for item in await self.store.list("issue_draft", MAX_DRAFTS)]

    async def get_draft(self, draft_id):
        if not isinstance(draft_id, str) or not _ID.fullmatch(draft_id):
            raise ValueError("反馈草稿编号不正确")
        item = await self.store.get("issue_draft", draft_id)
        if item is None:
            raise KeyError("反馈草稿不存在")
        return self._public(item)

    async def prepare_draft(self, repository, title, body):
        repository = normalize_repository(repository)
        title, body = self._content(title, body)
        content_digest = _digest([repository, title, body])
        async with self._lock:
            def work(db):
                rows = [json.loads(row[0]) for row in db.execute(
                    "SELECT body FROM documents WHERE kind='issue_draft' ORDER BY updated ASC")]
                for row in rows:
                    if row.get("content_digest") == content_digest and row.get("status") != "failed":
                        return row  # Especially unknown/successful outcomes: no retry clone.
                # A definitive rejection (e.g. 401), or a cancellation before
                # any request, may be prepared again. This is only a new local
                # draft; it receives a new ticket and requires fresh approval.
                if len(rows) >= MAX_DRAFTS:
                    removable = next((row for row in rows if row.get("status")
                                      not in {"submitting", "uncertain"}), None)
                    if removable is None:
                        raise ValueError("有较多待核实的反馈，请先到 GitHub 检查提交结果")
                    db.execute("DELETE FROM documents WHERE kind='issue_draft' AND id=?", (removable["id"],))
                item = {"id": uuid4().hex, "repository": repository, "title": title, "body": body,
                        "status": "draft", "content_digest": content_digest,
                        "created_at": time.time(), "updated_at": time.time()}
                self.store._put(db, "issue_draft", item)
                return item
            result = await self.store.transaction(work)
        return self._public(result)

    async def prepare_submission(self, draft_id):
        async with self._lock:
            draft = await self.get_draft(draft_id)
            if draft["status"] != "draft":
                raise ValueError(_UNCERTAIN if draft["status"] in {"submitting", "uncertain"}
                                 else "此草稿已有提交记录，不能重复发送")
            _, policy = self._policy(draft["repository"])
            title, body = self._content(draft["title"], draft["body"])
            if (title, body) != (draft["title"], draft["body"]):
                # Known credentials may have changed since drafting. A new draft
                # is required instead of silently changing an approved preview.
                raise ValueError("反馈内容的脱敏结果已变化，请重新生成草稿")
            arguments = self._arguments(draft)
            fingerprint = _digest([arguments, policy])
            ticket = uuid4().hex
            expires = time.time() + APPROVAL_SECONDS
            self._tickets = {key: value for key, value in self._tickets.items()
                             if value["expires_at"] > time.time()}
            if len(self._tickets) >= 64:
                raise ValueError("待确认的反馈较多，请先处理已有确认")
            self._tickets[ticket] = {"draft_id": draft_id, "fingerprint": fingerprint, "expires_at": expires}
            return {"name": "submit_github_issue", "arguments": arguments,
                    "summary": f"向 {draft['repository']} 提交 Issue：{draft['title']}",
                    "read_only": False, "fingerprint": fingerprint,
                    "expires_at": expires, "_issue_ticket": ticket}

    async def _finish(self, draft_id, status, *, error=None, url=None, number=None):
        def work(db):
            row = db.execute("SELECT body FROM documents WHERE kind='issue_draft' AND id=?", (draft_id,)).fetchone()
            if row is None:
                raise RuntimeError("反馈提交记录不存在")
            draft = json.loads(row[0])
            draft.update(status=status, updated_at=time.time())
            if error:
                draft["error"] = error
            if url:
                draft.update(url=url, number=number)
            self.store._put(db, "issue_draft", draft)
            return self._public(draft)
        return await self.store.transaction(work)

    async def submit(self, prepared):
        """Called only after the administrator executor's real confirmation."""
        draft_id = None
        async with self._lock:
            if not isinstance(prepared, dict) or prepared.get("name") != "submit_github_issue":
                raise ValueError("Issue 提交确认无效")
            ticket = self._tickets.get(prepared.get("_issue_ticket"))
            current = time.time()
            approved_until = prepared.get("_approval_expires")
            if (ticket is None or type(approved_until) not in (int, float)
                    or not math.isfinite(approved_until) or approved_until <= current
                    or approved_until > current + APPROVAL_SECONDS + 1
                    or ticket["expires_at"] <= current):
                raise PermissionError("Issue 提交确认已失效，请重新确认")
            draft_id = ticket["draft_id"]
            draft = await self.get_draft(draft_id)
            if draft["status"] != "draft":
                raise ValueError(_UNCERTAIN if draft["status"] in {"submitting", "uncertain"}
                                 else "此草稿已有提交记录，不能重复发送")
            token, policy = self._policy(draft["repository"])
            arguments = self._arguments(draft)
            fingerprint = _digest([arguments, policy])
            title, body = self._content(draft["title"], draft["body"])
            if ((title, body) != (draft["title"], draft["body"])
                    or prepared.get("arguments") != arguments
                    or prepared.get("fingerprint") != fingerprint
                    or ticket["fingerprint"] != fingerprint):
                raise PermissionError("反馈内容或授权已变化，请重新确认")
            http = getattr(getattr(self.deps.runtime, "services", None), "http", None)
            if http is None or not callable(getattr(http, "stream", None)):
                raise RuntimeError("系统 HTTP 服务不可用，反馈尚未提交")
            # Atomic and durable BEFORE any network request. A second executor,
            # cancellation or restart can never replay this draft.
            def claim(db):
                row = db.execute("SELECT body FROM documents WHERE kind='issue_draft' AND id=?", (draft_id,)).fetchone()
                saved = json.loads(row[0]) if row else None
                if saved is None or saved.get("status") != "draft" or self._arguments(saved) != arguments:
                    raise ValueError("反馈内容或提交状态已变化，请重新检查")
                saved.update(status="submitting", submission_fingerprint=fingerprint, updated_at=time.time())
                self.store._put(db, "issue_draft", saved)
            try:
                await self.store.transaction(claim)
            except asyncio.CancelledError:
                # The store deliberately finishes its transaction before
                # propagating cancellation. Do not leave a committed claim
                # looking in progress until the next process restart.
                self._tickets.pop(prepared["_issue_ticket"], None)
                try:
                    saved = await self.store.get("issue_draft", draft_id)
                    if saved and saved.get("status") == "submitting":
                        await self._finish(draft_id, "failed", error="操作已停止，未发送请求")
                except BaseException:
                    pass  # Persisted claims still recover fail-closed.
                raise
            self._tickets.pop(prepared["_issue_ticket"], None)

        try:
            # Recheck after waiting for the durable transaction, immediately
            # before beginning the external request.
            fresh_token, fresh_policy = self._policy(arguments["repository"])
            if (fresh_policy != policy or fresh_token != token or time.time() >= approved_until
                    or time.time() >= ticket["expires_at"]):
                await self._finish(draft_id, "failed", error="提交前授权发生变化，未发送请求")
                raise PermissionError("提交前授权发生变化，未发送请求")
            status, data = await self._post(http, arguments, token)
            if status == 201:
                number, url = self._acknowledgement(data, arguments["repository"])
                try:
                    await self._finish(draft_id, "succeeded", url=url, number=number)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise RuntimeError(_UNCERTAIN) from None
                return {"status": "succeeded", "url": url, "number": number}
            if status in {400, 401, 403, 404, 405, 410, 413, 415, 422, 429}:
                error = {401: "GitHub 认证失败，请管理员检查 Token",
                         403: "GitHub 拒绝提交，请管理员检查权限或限流状态",
                         404: "GitHub 仓库不可访问，请管理员检查仓库和 Token 权限",
                         422: "GitHub 未接受反馈，请检查内容或重复提交限制",
                         429: "GitHub 请求过于频繁，请稍后由管理员重新处理"}.get(status, "GitHub 未接受反馈")
                await self._finish(draft_id, "failed", error=error)
                return {"status": "failed", "error": error}
            raise RuntimeError(_UNCERTAIN)
        except BaseException as exc:
            # Even ConnectTimeout is terminal: do not rely on transport error
            # subtype to prove that no proxy/upstream accepted the POST.
            try:
                saved = await self.store.get("issue_draft", draft_id)
                if saved and saved.get("status") == "submitting":
                    await self._finish(draft_id, "uncertain", error=_UNCERTAIN)
            except BaseException:
                pass  # A persisted submitting record is recovered as uncertain.
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, PermissionError):
                raise PermissionError("提交前授权发生变化，未发送请求") from None
            raise RuntimeError(_UNCERTAIN) from None

    @staticmethod
    async def _post(http, arguments, token):
        endpoint = f"https://api.github.com/repos/{arguments['repository']}/issues"
        payload = bytearray()
        async with http.stream(
                "POST", endpoint, timeout=30, follow_redirects=False,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28", "Accept-Encoding": "identity"},
                json={"title": arguments["title"], "body": arguments["body"]}) as response:
            status = response.status_code
            if status != 201:
                return status, None  # Never parse or echo upstream errors.
            if response.headers.get("content-encoding", "").strip().lower() not in {"", "identity"}:
                raise ValueError("Unexpected response encoding")
            async for chunk in response.aiter_bytes(chunk_size=16384):
                if len(payload) + len(chunk) > 128 * 1024:
                    raise ValueError("Oversized issue acknowledgement")
                payload.extend(chunk)
        return status, json.loads(payload)

    @staticmethod
    def _acknowledgement(data, repository):
        if not isinstance(data, dict) or type(data.get("number")) is not int or not 0 < data["number"] < 2 ** 53:
            raise ValueError("Invalid issue acknowledgement")
        value = data.get("html_url")
        if not isinstance(value, str) or len(value) > 512:
            raise ValueError("Invalid issue URL")
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment
                or parsed.path.lower() != f"/{repository}/issues/{data['number']}"):
            raise ValueError("Unexpected issue URL")
        return data["number"], value
