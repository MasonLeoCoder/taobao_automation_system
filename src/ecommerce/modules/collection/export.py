"""采集批次的本地 JSON/CSV 导出；CSV 文本防止被表格软件当成公式。"""
import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BEIJING_TIME = ZoneInfo("Asia/Shanghai")

COLUMNS = (
    ("batch_id", "批次编号"), ("captured_at", "采集时间"), ("keyword", "关键词"),
    ("page_number", "页码"), ("sort_label", "页面排序"), ("slot_index", "原始格子位置"), ("product_index", "商品序号"),
    ("product_id", "商品ID"), ("title", "商品标题"), ("price_text", "展示价格"),
    ("price_value", "价格数值"), ("price_label", "价格说明"), ("sales_text", "销量提示原文"),
    ("sales_count", "销量展示数量"), ("sales_precision", "销量数量精度"), ("sales_metric", "销量口径"),
    ("sales_period", "销量周期"), ("sales_parse_status", "销量解析状态"), ("sales_parser_version", "销量解析规则版本"),
    ("shop_name", "店铺名称"), ("location_text", "页面地区"), ("product_url", "商品链接"),
    ("link_status", "链接来源"),
    ("main_image_url", "主图链接"), ("list_image_status", "主图下载状态"),
    ("list_image_path", "主图本地路径"), ("currency_symbol", "货币符号"),
    ("price_integer_text", "价格整数原文"), ("price_fraction_text", "价格小数原文"),
    ("shop_url", "店铺链接"), ("rank_title", "榜单名称"), ("rank_position_text", "榜单名次原文"),
    ("rank_position", "榜单名次"), ("attribute_tags", "属性标签"), ("service_tags", "优惠及服务标签"),
    ("video_present", "视频标识"), ("video_url", "视频链接"), ("video_icon_url", "视频图标链接"), ("title_icon_url", "标题前图标链接"),
    ("customer_service_icon_url", "客服图标链接"), ("sku_reference", "链接SKU ID线索"),
    ("ad_recommendation", "广告推荐位"), ("ad_label", "广告标记"), ("ad_status", "广告识别状态"),
    ("ad_basis", "广告识别依据"), ("identity_status", "商品ID状态"),
    ("missing_fields", "缺失字段"), ("field_state_text", "字段状态说明"), ("notes", "备注"),
)
DISPLAY = {
    "ad_recommendation": {True: "是", False: "未检测到指定标记"},
    "ad_basis": {"specified_image_url": "命中用户确认的广告标识图片"},
    "video_present": {True: "有", False: "未检测到视频标识"},
    "ad_status": {"identified": "已识别广告标记", "not_identified": "未识别，不代表非广告"},
    "identity_status": {"verified": "已确认", "unconfirmed": "未确认"},
    "link_status": {"canonicalized_from_list_link": "列表直链去除跟踪参数",
                    "constructed_from_corroborated_id": "根据交叉确认ID生成，未打开验证",
                    "constructed_from_dom_id": "根据DOM商品ID生成，未打开验证", "unconfirmed": "未确认"},
}
FIELD_NAMES = {"product_id": "商品ID", "product_url": "商品链接", "shop_name": "店铺名称",
               "price_value": "价格数值", "currency_symbol": "货币符号", "sales_text": "销量提示原文",
               **dict(COLUMNS)}
FIELD_STATES = {"provided": "已读取", "not_provided": "页面未提供", "not_identified": "元素或内容未识别", "not_applicable": "不适用"}


def csv_value(value, *, identifier=False):
    if value is None:
        return ""
    if isinstance(value, list):
        value = "；".join(value)
    value = str(value)
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    if identifier and value.isdigit() and len(value) > 15:
        return "'" + value
    return value


def format_capture_time(value):
    """CSV 使用北京时间；缺少时区或无法解析时保留原值，避免猜测。"""
    if not isinstance(value, str) or not value:
        return value
    try:
        captured = datetime.fromisoformat(value)
    except ValueError:
        return value
    if captured.tzinfo is None:
        return value
    return captured.astimezone(BEIJING_TIME).strftime("%Y-%m-%d %H:%M:%S")


def save_batch(folder, payload):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    json_path = folder / f"{payload['batch_id']}.json"
    csv_path = json_path.with_suffix(".csv")
    if json_path.exists() or csv_path.exists():
        raise FileExistsError("该采集批次已存在，未覆盖历史记录")
    json_temp, csv_temp = json_path.with_suffix(".json.tmp"), csv_path.with_suffix(".csv.tmp")
    source = {key: payload.get(key) for key in ("batch_id", "captured_at", "keyword", "page_number", "sort_label")}
    with csv_temp.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow([title for key, title in COLUMNS])
        for item in payload["items"]:
            record = {**source, **item}
            record['list_image_status']=(item.get('list_image') or {}).get('status')
            record['list_image_path']=(item.get('list_image') or {}).get('image_path')
            record["captured_at"] = format_capture_time(record.get("captured_at"))
            for key, labels in DISPLAY.items():
                record[key] = labels.get(record.get(key), record.get(key))
            record["missing_fields"] = [FIELD_NAMES.get(key, key) for key in record.get("missing_fields", [])]
            record["field_state_text"] = [FIELD_NAMES.get(key, key) + "：" + FIELD_STATES[state]
                                           for key, state in item.get("field_states", {}).items() if state != "provided"]
            writer.writerow([csv_value(record.get(key), identifier=key in {"product_id", "sku_reference"}) for key, title in COLUMNS])
    json_temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_temp.replace(csv_path)
    json_temp.replace(json_path)
    return json_path, csv_path
