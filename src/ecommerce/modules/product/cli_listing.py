"""命令行填写与入仓的业务流水，沿用公共任务存储检查点。"""
from datetime import datetime

from ecommerce.infrastructure.tasks.events import current_events, storage_checkpoint
from ecommerce.modules.account.managed import verify_editor_session
from ecommerce.platforms.qianniu.product_review import read_product_fields
from ecommerce.shared.exceptions import NeedsAttention
from ecommerce.shared.time import BEIJING
from .listing_history import DUPLICATE_NOTICE, code_fingerprint, pending_submissions


class CliListing:
    """记录当前命令行任务的填写、提交前核对和明确回执。"""

    def __init__(self, settings, target, on_expiry, on_duplicate_review):
        self.settings = settings
        self.target = target
        self.on_expiry = on_expiry
        self.on_duplicate_review = on_duplicate_review
        self.events = current_events()
        self.fields = None
        self.intent = False

    def _observed(self, page, identity):
        fields = read_product_fields(page)
        if not fields['title'].strip() or not fields['code']:
            raise NeedsAttention('无法核对商品标题和商品级商家编码，请检查千牛编辑页')
        return {'entry': 'cli', **fields, 'code_sha256': code_fingerprint(fields['code']), 'verified_identity': {
            'login_name': identity.login_name, 'platform_shop_id': identity.shop_id,
            'verified_at': datetime.now(BEIJING).isoformat(),
        }}

    def filled(self, page):
        identity = verify_editor_session(page, self.target, self.on_expiry)
        self.events.emit('listing_filled', '命令行已填写商品，尚未提交', details=self._observed(page, identity))

    def before_click(self, page):
        identity = verify_editor_session(page, self.target, self.on_expiry)
        self.fields = self._observed(page, identity)
        related = pending_submissions(self.settings, self.target['shop_id'], self.fields['code'],
                                      exclude_task=self.events.task_id)
        if not related:
            return
        links = '\n'.join(f'http://localhost:{self.settings.port}/tasks/{task_id}' for task_id in related)
        message = f'{DUPLICATE_NOTICE}\n{links}'
        if self.on_duplicate_review is None:
            raise NeedsAttention(message)
        self.on_duplicate_review(message)
        checked = self._observed(page, verify_editor_session(page, self.target, self.on_expiry))
        if any(checked[key] != self.fields[key] for key in ('title', 'code')):
            raise NeedsAttention('人工核对期间商品标题或商家编码发生变化，请重新核对后再提交')
        self.fields = checked

    def click_intent(self):
        if self.intent or self.fields is None:
            raise NeedsAttention('无法确认本次提交意图，请人工核对')
        operation = self.events.operation('event', 'listing_submit_intent',
                                          '已标记一次命令行入仓提交；异常只核对结果', details=self.fields)
        self.events.collection_barrier = self.events.append(operation)
        storage_checkpoint('记录商品提交意图')
        self.intent = True

    def result(self, result):
        if self.intent and result.status == 'succeeded':
            self.events.emit('listing_warehouse', result.message, details={**self.fields, 'item_id': result.item_id})
