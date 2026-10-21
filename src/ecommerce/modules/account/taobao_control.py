"""向当前淘宝任务发送暂停或停止请求，不启动第二个浏览器。"""
import json
import re
from uuid import uuid4

from ecommerce.infrastructure.storage.database import initialize, list_tasks


def request_control(settings, action):
    replies = {"continue": "继续", "confirm": "确认", "cancel": "取消"}
    if action not in {"pause", "stop", *replies}:
        raise ValueError("不支持的淘宝控制指令")
    initialize(settings.database)
    tasks = [task for task in list_tasks(settings.database) if task["name"].startswith(("account.taobao.", "collection.taobao."))
             and task["finished_at"] is None and task["status"] in {"running", "needs_attention"}]
    if not tasks:
        return "当前没有执行中的淘宝任务"
    if len(tasks) != 1:
        raise ValueError("发现多个未结束的淘宝任务，请先核对任务记录")
    task_id = tasks[0]["id"]
    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise ValueError("任务编号格式无效")
    directory = settings.workspace / "storage/task_controls"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}.json"
    request = {"action": action}
    if action in replies:
        state_path = path.with_suffix(".state.json")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("当前任务没有可接收反馈的提示，请等待任务提示后再操作") from error
        if not state.get("prompt_id") or replies[action] not in state.get("allowed", []):
            raise ValueError("当前提示不接受这个指令，请按任务提示输入继续、确认或取消")
        request["prompt_id"] = state["prompt_id"]
    temporary = directory / f"{task_id}-{uuid4().hex}.tmp"
    temporary.write_text(json.dumps(request), encoding="utf-8")
    temporary.replace(path)
    name = {"pause": "暂停", "stop": "停止", **replies}[action]
    return f"已发送{name}请求，等待当前淘宝任务的检查点接收；任务编号：{task_id}"
