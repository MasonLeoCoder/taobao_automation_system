"""只在安全HTML中将独立连续引文折叠，不修改Markdown、来源或校验结果。"""
from html.parser import HTMLParser
import re
from ecommerce.modules.review_analysis import v6_checks


class Blocks(HTMLParser):
    """保留安全HTML原始片段，读取顶层块及纯文字，仅用于展示分组。"""
    def __init__(self,rendered):
        """记录行偏移；参数：已安全渲染HTML；返回：解析器。"""
        super().__init__(convert_charrefs=True);self.rendered=rendered;self.offsets=[0]
        for match in re.finditer('\n',rendered):self.offsets.append(match.end())
        self.depth=0;self.current=None;self.blocks=[]

    def character_position(self):
        """计算原字符串位置；参数：无；返回：字符偏移。"""
        line,column=self.getpos();return self.offsets[line-1]+column

    def handle_starttag(self,tag,attrs):
        """采集顶层块和来源链接；参数：标签属性；返回：无。"""
        if self.depth==0:self.current={'start':self.character_position(),'tag':tag,'text':'','references':[]}
        if self.current is not None and tag=='a':
            href=dict(attrs).get('href','')
            if re.fullmatch(r'#review-[1-9][0-9]*',href):self.current['references'].append(href)
        if tag not in ('br','hr','img','input','meta','link'):self.depth+=1
        elif self.depth==0:self.finish(self.character_position()+len(self.get_starttag_text()))

    def handle_startendtag(self,tag,attrs):
        """处理闭合空标签；参数：标签属性；返回：无。"""
        self.handle_starttag(tag,attrs)

    def handle_endtag(self,tag):
        """保存完整顶层块；参数：标签；返回：无。"""
        self.depth=max(0,self.depth-1)
        if self.depth==0:self.finish(self.rendered.find('>',self.character_position())+1)

    def handle_data(self,value):
        """收集解码后的文字用于保守识别；参数：文字；返回：无。"""
        if self.current is not None:self.current['text']+=value

    def finish(self,end):
        """登记原HTML范围；参数：结束位置；返回：无。"""
        if self.current is not None:self.current['end']=end;self.blocks.append(self.current);self.current=None


def citation_count(block):
    """只识别整段独立引文，混入分析则不收起；参数：HTML块；返回：可折叠引用数。"""
    if block['tag']!='p' or not block['references']:return 0
    text=block['text'];cursor=0;count=0
    for marker in v6_checks.MARKER.finditer(text):
        if marker.start()<cursor:continue
        if text[cursor:marker.start()].strip(' \t\r\n。；;'):return 0
        identity=v6_checks.metadata(marker[1]);quote,end=v6_checks.quoted(text,marker.end())
        if identity is None or quote is None:return 0
        if '#review-'+str(identity['review_id']) not in block['references']:return 0
        count+=1;cursor=end
    if text[cursor:].strip(' \t\r\n。；;') or count!=len(block['references']):return 0
    return count


def collapse(rendered):
    """按相邻独立引用组添加原生折叠；参数：安全HTML；返回：仅展示用HTML。"""
    parser=Blocks(rendered);parser.feed(rendered);parser.close();blocks=parser.blocks
    parts=[];cursor=0;index=0;previous=None
    while index<len(blocks):
        block=blocks[index];count=citation_count(block)
        parts.append(rendered[cursor:block['start']])
        if not count:
            parts.append(rendered[block['start']:block['end']]);previous=len(parts)-1
            cursor=block['end'];index+=1;continue
        # 只合并紧邻引文前的展示提示，不删除任何分析内容或写回原稿。
        if previous is not None and index and blocks[index-1]['tag']=='p' and not rendered[blocks[index-1]['end']:block['start']].strip():
            parts[previous]=re.sub(r'原文依据[：:]\s*</p>$','</p>',parts[previous])
            if parts[previous]=='<p></p>':parts[previous]=''
        end=block['end'];index+=1
        while index<len(blocks) and not rendered[end:blocks[index]['start']].strip():
            following=citation_count(blocks[index])
            if not following:break
            count+=following;end=blocks[index]['end'];index+=1
        parts.append('<details class="ui-report-evidence"><summary>查看原文依据（'+str(count)+'处引用）</summary><div class="ui-report-evidence-body">'+rendered[block['start']:end]+'</div></details>')
        cursor=end;previous=None
    parts.append(rendered[cursor:]);return ''.join(parts)
