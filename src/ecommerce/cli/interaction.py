"""CLI 公共人工验证交互。"""
from ecommerce.shared.exceptions import NeedsAttention


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
    while True:
        try:
            prompt = "输入“确认”或“取消”并回车：" if native else "手动处理完成后输入“继续”并回车：" if popup else "手动验证完成后输入“继续”并回车："
            feedback = input(prompt).strip()
        except EOFError as error:
            raise NeedsAttention("输入通道已关闭，未收到人工处理反馈") from error
        if native and feedback in {"确认", "取消"}:
            return feedback
        if not native and feedback in {"继续", "已完成", "完成", "continue"}:
            return "继续"
        print("尚未收到明确反馈，保持暂停。", flush=True)


def prompt_submission_review(message: str) -> None:
    """提醒并等待提交检查；参数：错误和截图说明；返回：明确反馈后返回，无超时。"""
    _play_attention_sound()
    print(message, flush=True)
    while True:
        try:
            feedback = input("人工检查完成后输入“继续”并回车：").strip()
        except EOFError as error:
            raise NeedsAttention("输入通道已关闭，未收到商品提交检查反馈") from error
        if feedback in {"继续", "已完成", "完成", "continue"}:
            return
        print("尚未收到检查完成反馈，保持浏览器打开并等待。", flush=True)


