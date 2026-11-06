"""单商品网页后台执行；已启动的导入单只运行一次，进程退出后不重放。"""
from dataclasses import replace
import json
from logging import getLogger
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError
from ecommerce.infrastructure.tasks.events import current_events, storage_checkpoint
from ecommerce.infrastructure.tasks.lock import shop_lock
from ecommerce.infrastructure.tasks.ownership import atomic_json, owner_alive, process_started_at
from ecommerce.infrastructure.tasks.runner import current_task, run_task
from ecommerce.platforms.qianniu.product_review import read_listing_fields, read_listing_media
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped
from .service import open_publish
from .listing_logs import log_page
from .listing_logs import local_log
from .web_materials import digest, import_materials

_ID = re.compile('[0-9a-f]{32}')
_ACTIVE = {'queued', 'running', 'waiting'}


class ListingConflict(ValueError):
    """请求与当前上架任务状态不符。"""


class ListingWorkbench:
    """保留本环境导入快照、后台任务进度及单次人工反馈。"""

    def __init__(self, settings):
        self.settings = replace(settings.for_platform('qianniu'), headless=False)
        self.root = settings.workspace / 'storage' / 'product_listing'

    def _bundle(self, identifier):
        """核对导入归属；参数：编号；返回：当前环境下已存在的导入目录。"""
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise LookupError('找不到这份商品资料')
        path = self.root / identifier
        if path.resolve().parent != self.root.resolve() or not (path / 'manifest.json').is_file():
            raise LookupError('找不到这份商品资料')
        return path

    def preflight(self, uploads, **form):
        """执行离线预检；参数：文件和导航信息；返回：错误或已封存资料。"""
        return import_materials(self.root, uploads, **form)

    def snapshot(self, identifier):
        """读当前资料和任务状态；参数：导入编号；返回：不含内部路径的网页结果。"""
        path = self._bundle(identifier)
        manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
        manifest.pop('hashes')
        state = path / 'state.json'
        task = json.loads(state.read_text(encoding='utf-8')) if state.is_file() else None
        if task and task['phase'] in _ACTIVE and owner_alive(task['pid'], task['process_started']) is False:
            task.update(phase='interrupted', prompt=None,
                        message='原执行进程已退出。未恢复浏览器操作；如已点击提交，请核对千牛仓库。')
        if task:
            task = {key: value for key, value in task.items() if key not in {'pid', 'process_started'}}
        return {**manifest, 'task': task}

    def start(self, identifier, submit):
        """领取一份已确认的预检快照；参数：编号和明确布尔值；返回：现有或新任务状态。"""
        if type(submit) is not bool:
            raise ValueError('请选择本次是否提交到仓库')
        if submit and self.settings.environment == 'verify':
            raise ValueError('自动验证环境不执行真实入仓')
        path = self._bundle(identifier)
        with shop_lock(path / 'launch'):
            if (path / 'state.json').exists():
                return self.snapshot(identifier)
            state = {'phase': 'queued', 'outcome': None, 'task_id': None, 'item_id': None,
                     'submit': submit, 'submit_attempted': False, 'prompt': None, 'evidence': None,
                     'message': '等待后台取得千牛会话', 'pid': os.getpid(),
                     'process_started': process_started_at(os.getpid())}
            atomic_json(path / 'state.json', state)
            worker = _ListingRun(self.settings, path, state)
            try:
                threading.Thread(target=worker.run, daemon=True, name='listing-'+identifier[:8]).start()
            except RuntimeError:
                worker.update(phase='failed', message='后台任务启动失败，未打开千牛')
                raise
        return self.snapshot(identifier)

    def feedback(self, identifier, prompt_id, action):
        """仅接受当前人工提示的单次操作；参数：导入编号、提示编号及决定；返回：当前状态。"""
        path = self._bundle(identifier)
        with shop_lock(path / 'control'):
            task = self.snapshot(identifier)['task']
            prompt = task and task['prompt']
            if not prompt or prompt['id'] != prompt_id or action not in prompt['actions']:
                raise ListingConflict('提示已改变或操作不适用，请刷新后重新核对')
            target = path / f'feedback-{prompt_id}.json'
            if target.exists():
                raise ListingConflict('本次反馈已收到，请等待页面复查')
            atomic_json(target, {'action': action})
        return self.snapshot(identifier)

    def evidence(self, identifier, version):
        """定位本任务签出的截图；参数：导入编号和截图版本；返回：文件。"""
        path = self._bundle(identifier)
        task = self.snapshot(identifier)['task']
        if not task or not _ID.fullmatch(version) or task['evidence'] != version:
            raise LookupError('当前没有可查看的截图')
        evidence = path / f'{version}.png'
        if not evidence.is_file():
            raise LookupError('当前没有可查看的截图')
        return evidence

    def logs(self, identifier, *, after=None, before=None):
        """仅查询当前导入绑定的任务事件；参数：导入编号和游标；返回：日志页。"""
        path = self._bundle(identifier)
        task = self.snapshot(identifier)['task']
        return log_page(self.settings, path, task and task['task_id'], after=after, before=before)


class _ListingRun:
    """一份预检快照的单个后台执行；网页请求不会直接操作浏览器。"""

    def __init__(self, settings, path, state):
        self.settings, self.path, self.state = settings, path, state
        self.manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
        self.preview = self.manifest['preview']
        self.media, self.notes, self.attributes = {}, [], {}
        self.sku_media = None
        self.click_in_progress = False
        self.local_log = None

    def update(self, **values):
        """原子保存当前进度；参数：状态更新；返回：无。"""
        next_state = {**self.state, **values}
        atomic_json(self.path / 'state.json', next_state)
        self.state = next_state

    def progress(self, message):
        """展示正在执行的业务步骤；参数：步骤；返回：无。"""
        self.update(phase='running', message=message, prompt=None)
        if session := current_events():
            session.progress('running', message)
        elif self.local_log:
            self.local_log.write(message)

    def login_wait(self, page, *, retry=False):
        """等待明确扫码反馈；参数：登录页和是否复核失败；返回：收到完成，由平台复核。"""
        message = ('尚未确认登录。若二维码已过期，请在千牛窗口刷新二维码后扫码。' if retry
                   else '请用手机千牛 App 扫描已打开的千牛窗口中的二维码。')
        # 登录二维码仅保留在千牛窗口，不写入截图或网页接口。
        self._wait(message + ' 完成登录后，回到网页点击「完成」。', ['continue'], kind='login', context=page.context)

    def _screenshot(self, *, page=None, message=''):
        """保存当前截图或引用已由千牛守卫生成的证据；返回：版本号或空。"""
        version = uuid4().hex
        target = self.path / f'{version}.png'
        try:
            if page is not None and not page.is_closed():
                page.screenshot(path=str(target), timeout=3500, animations='disabled')
                return version
            source = re.search(r'^截图[：:]\s*(.+\.png)\s*$', message, re.M)
            if source:
                path = Path(source.group(1)).resolve()
                if path.parent == (self.settings.log_dir / 'screenshots').resolve() and path.stat().st_size <= 12 * 1024**2:
                    target.write_bytes(path.read_bytes())
                    return version
        except (OSError, PlaywrightError):
            getLogger(__name__).warning('未能保存千牛现场截图，保留人工提示')
        return None

    def _wait(self, message, actions, *, page=None, differences=None, kind='manual', context=None):
        """等待当前提示的专用反馈；返回：明确操作，关闭网页不算反馈。"""
        previous = self.state if self.state['phase'] == 'waiting' else None
        evidence = self._screenshot(page=page, message=message)
        message = (message.replace('输入“继续”', '点击「完成」').replace('反馈“继续”', '点击「完成」')
                   .replace('输入“确认”', '点击「确认弹窗」').replace('输入“取消”', '点击「取消弹窗」'))
        prompt = {'id': uuid4().hex, 'kind': kind, 'actions': actions, 'differences': differences or [],
                  'evidence': evidence}
        self.update(phase='waiting', prompt=prompt, message=message,
                    evidence=evidence or self.state['evidence'])
        if session := current_events():
            session.progress('needs_attention', message)
        elif self.local_log:
            self.local_log.write(message, 'WARNING', '等待人工处理')
        feedback = self.path / f'feedback-{prompt["id"]}.json'
        while not feedback.exists():
            if context is not None:
                pages = [candidate for candidate in context.pages if not candidate.is_closed()]
                if not pages:
                    raise NeedsAttention('千牛浏览器已关闭，无法继续扫码登录')
                try:
                    pages[-1].wait_for_timeout(250)
                except PlaywrightError:
                    if not any(not candidate.is_closed() for candidate in context.pages):
                        raise NeedsAttention('千牛浏览器已关闭，无法继续扫码登录') from None
            elif page is not None:
                if page.is_closed():
                    raise NeedsAttention('千牛页面已关闭，无法继续本次操作')
                page.wait_for_timeout(250)
            else:
                time.sleep(.25)
        action = json.loads(feedback.read_text(encoding='utf-8'))['action']
        if action not in actions:
            raise NeedsAttention('人工反馈不属于当前提示')
        storage_checkpoint('收到网页人工反馈后复核')
        if previous:
            self.update(prompt=previous['prompt'], message=previous['message'], evidence=previous['evidence'])
        else:
            self.progress('已收到反馈，正在复核当前步骤')
        return action

    def verification(self, request):
        """保留平台验证现场；参数：验证请求；返回：收到继续后交守卫复查。"""
        if self.click_in_progress:
            self._wait('提交点击期间出现验证，无法确定是否已提交。请先核对仓库；程序不会继续这次点击。',
                       ['finish'], page=request.page)
            raise NeedsAttention('提交结果待核实，未重放提交')
        self._wait(f'{request.step}：请在千牛窗口手动处理验证，处理后回网页点击「完成」。', ['continue'], page=request.page)

    def attention(self, message):
        """处理千牛普通及原生弹窗；参数：守卫说明；返回：本次明确决定。"""
        native = message.startswith(('【需要人工决定原生弹窗】', '【原生弹窗尚未决定】'))
        if self.click_in_progress and not native:
            self._wait(f'{message}\n提交点击期间出现中断，请核对仓库，程序不会重放提交。', ['finish'])
            raise NeedsAttention('提交结果待核实，未重放提交')
        answer = self._wait(message, ['confirm', 'cancel'] if native else ['continue'], kind='native' if native else 'manual')
        return {'confirm': '确认', 'cancel': '取消', 'continue': '继续'}[answer]

    def submission_attention(self, message):
        """提示提交前或提交后核对；参数：说明；返回：收到反馈后复查，不重复点击。"""
        actions = ['continue', 'finish'] if self.state['outcome'] == 'uncertain' else ['continue']
        if self._wait(message, actions, kind='receipt' if self.state['submit_attempted'] else 'manual') == 'finish':
            raise NeedsAttention('提交结果待核实，未重复提交')

    def record_media(self, page, group):
        """记录刚核对过的媒体；参数：商品页和分组；返回：无。"""
        self.media[group] = read_listing_media(page, group)

    def filled(self, page, categories, sku, attributes):
        """汇总填写证据；参数：页面、分类、SKU媒体和属性结果；返回：等待或继续入仓。"""
        for label, result in attributes.items():
            if result:
                self.attributes[label] = result.selected
            if result and result.fallback_reason:
                self.notes.append(f'{label}：资料为「{self.preview["attributes"][label] or "未指定"}」，千牛选择「{result.selected}」；{result.fallback_reason}')
        if categories:
            self.notes.extend(f'店铺分类「{name}」：{reason}' for name, reason in categories.skipped.items())
        if sku:
            for name, skipped in sku.skipped.items():
                for label, reason in skipped.items():
                    planned = self.preview['guide'] if label == 'SKU搜索标题' else self.preview['sku_images'].get(name)
                    if planned:
                        self.notes.append(f'SKU「{name}」{label}未按预检完成：{reason}')
        self.sku_media = read_listing_fields(page)['sku_media']
        self.update(outcome='filled', notes=self.notes, message='已填写，尚未提交')
        if not self.state['submit']:
            self._wait('已填写，未提交。可在千牛检查，检查完点击「结束检查」。', ['finish'], page=page, kind='finish')

    def _comparison(self, page):
        """比较预检与当前页面；参数：编辑页；返回：实际快照及差异。"""
        actual = read_listing_fields(page)
        actual['media'] = {key: read_listing_media(page, key) for key in self.media}
        lines = list(self.notes)
        for key, label in (('title', '宝贝标题'), ('guide', '导购标题'), ('price', '一口价'), ('code', '商家编码')):
            if actual[key] != self.preview[key]:
                lines.append(f'{label}：确认值「{self.preview[key]}」，页面值「{actual[key]}」')
        if actual['skus'] != self.preview['skus']:
            for index in range(max(len(actual['skus']), len(self.preview['skus']))):
                expected = self.preview['skus'][index] if index < len(self.preview['skus']) else None
                observed = actual['skus'][index] if index < len(actual['skus']) else None
                if expected != observed:
                    lines.append(f'SKU 第{index+1}行：确认值 {expected or "无"}；页面值 {observed or "无"}')
        if actual['sku_media'] != self.sku_media:
            lines.append('SKU 图片或搜索标题在填写完成后发生变化')
        for label, value in self.attributes.items():
            current = actual['attributes'][label]
            if value != current:
                lines.append(f'{label}：填写值「{value}」，当前页「{current or "未核实"}」')
        for key, label in (('main', '1:1 主图'), ('portrait', '3:4 主图'), ('detail', '详情图'), ('video', '视频')):
            if key in self.media and actual['media'][key] != self.media[key]:
                lines.append(f'{label}与填写时核对的素材或顺序不同')
        return actual, lines

    def before_click(self, page):
        """核对实际商品页并等待差异批准；参数：提交页；返回：仅已核对时放行。"""
        actual, differences = self._comparison(page)
        while True:
            if differences:
                action = self._wait('填写结果存在差异。请在千牛核对下列内容，再决定是否提交到仓库。',
                                    ['approve', 'fill_only'], page=page, differences=differences, kind='review')
                if action == 'fill_only':
                    self._wait('已填写，未提交。检查完成后可关闭千牛窗口。', ['finish'], page=page, kind='finish')
                    raise TaskStopped('已填写，用户选择不提交')
            storage_checkpoint('提交到仓库前核对任务状态')
            updated, new_differences = self._comparison(page)
            if actual == updated and differences == new_differences:
                return
            actual, differences = updated, new_differences

    def click_intent(self):
        """持久标记一次不可重放的提交意图；参数：无；返回：写入成功后允许点击。"""
        if self.state['submit_attempted']:
            raise NeedsAttention('已记录提交意图，只能核对结果，不会重复提交')
        self.update(submit_attempted=True, outcome='uncertain', message='正在提交到仓库并核对结果')
        if session := current_events():
            operation = session.operation('event', 'listing_submit_intent', '已标记一次入仓提交；异常只核对结果')
            session.collection_barrier = session.append(operation)
            storage_checkpoint('记录商品提交意图')
        self.click_in_progress = True

    def click_finished(self):
        """结束不可重放的单次点击窗口；随后只读取提交结果。"""
        self.click_in_progress = False

    def result(self, result):
        """记录平台明确回执；参数：核对结果；返回：状态，未确认不假报成功。"""
        if result.status == 'succeeded':
            evidence = self._screenshot(page=result.page)
            self.update(outcome='warehouse', item_id=result.item_id, message=result.message,
                        evidence=evidence or self.state['evidence'])
            if session := current_events():
                session.emit('listing_warehouse', result.message, details={'item_id': result.item_id})

    def run(self):
        """在公共任务执行器中运行已封存资料；参数：无；返回：持久任务结果。"""
        with local_log(self.settings, self.path) as self.local_log:
            self._run()

    def _run(self):
        """完成本次执行并保留结果；参数：无；返回：无。"""
        def work():
            """执行前验证文件摘要并沿用现有填写；返回：任务说明。"""
            self.update(task_id=current_task()[1])
            self.progress('正在核对已确认的素材版本')
            files = self.manifest['files']
            for key, names in files.items():
                directory = self.path / key
                if not names:
                    continue
                if {p.name for p in directory.iterdir()} != set(names):
                    raise NeedsAttention('导入文件清单已改变，请重新选择文件夹并自检')
                for name in names:
                    file = directory / name
                    if file.resolve().parent != directory.resolve() or digest(file) != self.manifest['hashes'][f'{key}/{name}']:
                        raise NeedsAttention(f'已确认的文件发生变化：{name}；请重新自检')
            return open_publish(self.settings, keyword=self.preview['keyword'], category=self.preview['category'],
                brand=self.preview['brand'], main_images_dir=str(self.path / 'main'),
                portrait_images_dir=str(self.path / 'portrait') if files['portrait'] else None,
                detail_images_dir=str(self.path / 'detail') if files['detail'] else None,
                sku_images_dir=str(self.path / 'sku'), product_info_file=str(self.path / 'excel' / files['excel'][0]),
                video_file=str(self.path / 'video' / files['video'][0]) if files['video'] else None,
                submit=self.state['submit'], on_attention=self.attention,
                on_submit_attention=self.submission_attention, web_review=self)
        try:
            result = run_task(self.settings, 'product.submit' if self.state['submit'] else 'product.open', work)
            message = ('提交结果待核实；请检查千牛仓库，未重复提交' if self.state['outcome'] == 'uncertain'
                       else result.message)
            if self.local_log:
                self.local_log.write(message, 'INFO' if result.status == 'succeeded' else 'WARNING', '任务结束')
            self.update(phase=result.status, prompt=None, message=message, task_id=result.task_id)
        except Exception:
            getLogger(__name__).exception('网页上架后台执行中断')
            self.update(phase='failed', prompt=None, message='任务中断，请查看运行日志及实际页面')
