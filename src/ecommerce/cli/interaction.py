"""CLI 公共人工验证交互。"""
from ecommerce.shared.exceptions import NeedsAttention

def prompt_manual_verification(message: str) -> None:
    """提醒并等待明确反馈；参数：步骤及验证提示；返回：用户确认后返回，无超时。"""
    if message.startswith("【需要人工验证】"):
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except (ImportError, RuntimeError, OSError):
            print("\a", end="", flush=True)
    print(message, flush=True)
    while True:
        try:
            feedback = input("手动验证完成后输入“继续”并回车：").strip()
        except EOFError as error:
            raise NeedsAttention("输入通道已关闭，未收到验证完成反馈") from error
        if feedback in {"继续", "已完成", "完成", "continue"}:
            return
        print("尚未收到完成确认，保持暂停。输入“继续”后才检查是否可以恢复。", flush=True)


