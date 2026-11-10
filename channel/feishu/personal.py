"""个人定制实例的飞书可信入口；身份校验先于一切下载与模型调用。"""
import hashlib
import json
import uuid

import requests

from common.personal_tasks import OWNER, TaskError, TaskStore, enqueue
from config import conf


def binding_for(channel):
    binding = conf().get("personal_feishu_binding", {}) or {}
    instance = channel.instance_id or "feishu"
    if (not binding.get("sender_open_id") or not binding.get("tenant_id") or
            binding.get("channel_instance_id") != instance or
            binding.get("app_id") != channel.cfg("feishu_app_id")):
        raise TaskError("config_unavailable", "请在本机设置绑定本人飞书身份")
    return binding


def authorize(channel, event):
    binding = binding_for(channel)
    sender = event.get("sender") or {}
    msg = event.get("message") or {}
    if (msg.get("chat_type") != "p2p" or sender.get("sender_type", "user") != "user" or
            (sender.get("sender_id") or {}).get("open_id") != binding["sender_open_id"] or
            sender.get("tenant_key", event.get("tenant_key")) != binding["tenant_id"] or
            event.get("app_id", binding["app_id"]) != binding["app_id"]):
        raise TaskError("forbidden")
    return binding


def register(channel):
    from common.personal_worker import register_channel
    register_channel(channel.instance_id or "feishu", lambda store, task: download(channel, store, task),
                     lambda task, text: notify(channel, task, text))


def receive(channel, event):
    binding = authorize(channel, event)
    msg = event.get("message") or {}
    mid = msg.get("message_id")
    if not mid:
        raise TaskError("invalid_request")
    store = TaskStore()
    key = "feishu:" + binding["channel_instance_id"] + ":" + mid
    # 已登记的接收先于积压过滤恢复。
    existing = next((r for r in store.list() if r["request_id"] == key), None)
    if not existing:
        created = msg.get("create_time")
        if created and channel._startup_ts and int(created) / 1000 < channel._startup_ts:
            return
    from models.config_revision import capture_config_revision
    content = json.loads(msg.get("content") or "{}")
    text = content.get("text", "") if msg.get("message_type") == "text" else ""
    origin = dict(channel="feishu", channel_instance_id=binding["channel_instance_id"],
                  app_id=binding["app_id"], tenant_id=binding["tenant_id"], sender_open_id=binding["sender_open_id"],
                  message_id=mid, chat_id=msg.get("chat_id"), session_id="personal-feishu:" + binding["sender_open_id"])
    task = store.submit(OWNER, key, {"event": event, "text": text}, origin, capture_config_revision())
    register(channel)
    enqueue(task["task_id"])
    return task


def download(channel, store, task):
    event = task["payload"]["event"]
    authorize(channel, event)
    # 保持原会话通知归属；重新绑定不能接管旧任务。
    binding = binding_for(channel)
    if any(task["origin"].get(k) != binding.get(k) for k in ("app_id", "tenant_id", "sender_open_id", "channel_instance_id")):
        raise TaskError("forbidden")
    if task.get("download_complete"):
        return
    from channel.feishu.feishu_message import FeishuMessage
    from bridge.context import ContextType
    message = FeishuMessage(event, is_group=False, access_token=channel.fetch_access_token())
    if message.ctype == ContextType.FILE:
        message.prepare()
        raw = json.loads(event["message"].get("content") or "{}")
        store.attach(OWNER, task["task_id"], message.content, raw.get("file_name"), raw.get("file_key") or "0")
    elif getattr(message, "has_file_attachments", False):
        if message.failed_files:
            raise TaskError("download_failed")
        for i, (path, _) in enumerate(message.attachments):
            store.attach(OWNER, task["task_id"], path, attachment_id=str(i))
    elif message.ctype == ContextType.IMAGE:
        if not message.image_path:
            raise TaskError("download_failed")
        store.attach(OWNER, task["task_id"], message.image_path, attachment_id="0")
    # post 的正文必须沿用解析后的原始文字，且写入处理记录供重启使用。
    if task["payload"].get("text") != getattr(message, "post_text", task["payload"].get("text")):
        with store._transaction() as db:
            r = store._read(db, OWNER, task["task_id"])
            r["payload"]["text"] = message.post_text
            store._save(db, r)
    store._change(OWNER, task["task_id"], task["attempt"], {"running"}, download_complete=True)


def notify(channel, task, text):
    binding = binding_for(channel)
    if any(task["origin"].get(k) != binding.get(k) for k in ("app_id", "tenant_id", "sender_open_id", "channel_instance_id")):
        return "failed"
    # 飞书 uuid 幂等参数使用同一通知内容的稳定键，结果未知不重复业务。
    notification_key = str(uuid.uuid5(uuid.NAMESPACE_URL, task["task_id"] + ":" + hashlib.sha256(text.encode()).hexdigest()))
    try:
        res = requests.post("https://open.feishu.cn/open-apis/im/v1/messages", params={"receive_id_type": "open_id"},
                            headers={"Authorization": "Bearer " + channel.fetch_access_token()},
                            json={"receive_id": task["origin"]["sender_open_id"], "msg_type": "text",
                                  "content": json.dumps({"text": text}, ensure_ascii=False), "uuid": notification_key}, timeout=(5, 30))
        return "sent" if res.status_code == 200 and res.json().get("code") == 0 else "failed"
    except requests.RequestException:
        return "unknown"
