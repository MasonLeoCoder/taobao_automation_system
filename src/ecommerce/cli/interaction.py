"""CLI 公共人工验证交互。"""
from ecommerce.shared.exceptions import NeedsAttention


def _play_attention_sound() -> None:
    """播放人工处理提示音；参数：无；返回：系统提示音失败时使用终端铃声。"""
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except (ImportError, RuntimeError, OSError):
        print("\a", end="", flush=True)


def prompt_manual_verification(message: str) -> None:
    """提醒并等待明确反馈；参数：步骤及验证提示；返回：用户确认后返回，无超时。"""
    if message.startswith("【需要人工验证】"):
        _play_attention_sound()
    print(message, flush=True)
    while True:
        try:
            feedback = input("手动验证完成后输入“继续”并回车：").strip()
        except EOFError as error:
            raise NeedsAttention("输入通道已关闭，未收到验证完成反馈") from error
        if feedback in {"继续", "已完成", "完成", "continue"}:
            return
        print("尚未收到完成确认，保持暂停。输入“继续”后才检查是否可以恢复。", flush=True)


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


