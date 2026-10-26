"""基本信息独立完整性契约，不涉及浏览器或数据库。"""
FIELDS=('title','shop_name','sales_text','price_text')


def assessment(info,source_url,captured_at):
    """判断独立信息是否可发布；参数：实际字段、来源及时间；返回：状态、警告及阻断项。"""
    states=info.get('field_states') or {}
    issues=[]
    if not isinstance(info.get('title'),str) or not info['title'].strip() or states.get('title')=='ambiguous':
        issues.append({'code':'title_unconfirmed','message':'详情标题未唯一确认'})
    if not source_url:
        issues.append({'code':'source_unconfirmed','message':'详情来源未确认'})
    if not captured_at:
        issues.append({'code':'time_unconfirmed','message':'基本信息观察时间未记录'})
    warnings=[{'code':key+'_missing','message':{'shop_name':'详情店铺名称','sales_text':'详情销量原文','price_text':'页面展示价格'}[key]+'未确认或未展示'}
              for key in FIELDS[1:] if not info.get(key)]
    return {'status':'partial' if issues else 'complete','warnings':warnings,'blocking_issues':issues}
