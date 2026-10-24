"""CLI 公共人工验证交互。"""
from ecommerce.infrastructure.tasks.events import notify as print
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped


def _manual_state(state,message,decision=None):
    """保存人工等待及明确反馈；参数：状态、说明、受控决定；返回：无，不录制输入原文。"""
    from ecommerce.infrastructure.tasks.events import current_events,storage_checkpoint
    session=current_events()
    if session:
        if state=='running':
            storage_checkpoint('人工反馈后复核')
        session.progress(state,message)
        session.emit('manual_feedback' if decision else 'manual_wait',message,state=state,
                     details={'decision':decision} if decision else {})


def _play_attention_sound() -> None:
    """播放人工处理提示音；参数：无；返回：系统提示音失败时使用终端铃声。"""
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except (ImportError, RuntimeError, OSError):
        print("\a", end="", flush=True)


def prompt_manual_verification(message: str) -> str:
    """共用浏览器人工交互；参数：验证或弹窗提示；返回：明确反馈，不设超时。"""
    native = message.startswith("【需要人工决定原生弹窗】")
    popup = message.startswith(("【需要人工处理弹窗】", "【弹窗尚未处理】"))
    if message.startswith(("【需要人工验证】", "【需要人工处理弹窗】", "【需要人工决定原生弹窗】")):
        _play_attention_sound()
    print(message, flush=True)
    _manual_state('needs_attention',message)
    while True:
        try:
            prompt = "输入“确认”或“取消”并回车：" if native else "手动处理完成后输入“继续”并回车：" if popup else "手动验证完成后输入“继续”并回车："
            feedback = input(prompt).strip()
        except EOFError as error:
            raise NeedsAttention("输入通道已关闭，未收到人工处理反馈") from error
        if native and feedback in {"确认", "取消"}:
            _manual_state('running','已收到原生弹窗决定，等待页面复核',feedback)
            return feedback
        if not native and feedback in {"继续", "已完成", "完成", "continue"}:
            _manual_state('running','已收到人工反馈，等待页面复核','继续')
            return "继续"
        print("尚未收到明确反馈，保持暂停。", flush=True)


def prompt_submission_review(message: str) -> None:
    """提醒并等待提交检查；参数：错误和截图说明；返回：明确反馈后返回，无超时。"""
    _play_attention_sound()
    print(message, flush=True)
    _manual_state('needs_attention',message)
    while True:
        try:
            feedback = input("人工检查完成后输入“继续”并回车：").strip()
        except EOFError as error:
            raise NeedsAttention("输入通道已关闭，未收到商品提交检查反馈") from error
        if feedback in {"继续", "已完成", "完成", "continue"}:
            _manual_state('running','已收到提交检查反馈；核对现有结果，不重放提交','继续')
            return
        print("尚未收到检查完成反馈，保持浏览器打开并等待。", flush=True)


def prompt_taobao_attention(message: str) -> str:
    """淘宝严格反馈：明确继续或停止；原生弹窗单独决定确认或取消。"""
    native = message.startswith(("【需要人工决定原生弹窗】", "【原生弹窗尚未决定】"))
    if message.startswith(("【需要人工验证】", "【需要人工检查】", "【需要人工决定原生弹窗】")):
        _play_attention_sound()
    print(message, flush=True)
    while True:
        try:
            from ecommerce.platforms.taobao.verification import active_guard
            guard = active_guard()
            prompt = "输入“确认”/“取消”（或“停止”结束任务）：" if native else "处理完成后输入“继续”（或“停止”结束任务）："
            feedback = guard.read_feedback(prompt, allowed={"确认", "取消"} if native else {"继续"}) if guard is not None else input(prompt).strip()
        except EOFError as error:
            raise NeedsAttention("淘宝人工反馈输入通道已关闭，未收到继续指令") from error
        if feedback == "停止":
            raise TaskStopped("用户已停止淘宝任务，未继续后续操作")
        if native and feedback in {"确认", "取消"}:
            return feedback
        if not native and feedback == "继续":
            return feedback
        print("未收到有效指令，保持暂停。", flush=True)


