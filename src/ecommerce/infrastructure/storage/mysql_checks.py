"""比较本项目使用的有限 CHECK 表达式，忽略反射多余括号但保留运算优先级。"""
import re


def check_expression_tree(expression):
    """解析项目 CHECK 运算；参数：SQL 文本；返回：可比较语法树，不支持的语法报错。"""
    expression=re.sub(r"_(?:ascii|utf8mb4|utf8mb3|utf8|binary)(?=')",'',expression,flags=re.I)
    pattern=re.compile(r"\s+|`[^`]+`|'(?:''|[^'])*'|\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_]*|<=|>=|<>|!=|[=<>+(),]")
    tokens=[]
    position=0
    while position<len(expression):
        match=pattern.match(expression,position)
        if not match:
            raise ValueError('检查约束包含未支持的表达式')
        token=match.group()
        if not token.isspace():
            tokens.append(token if token.startswith("'") else token.strip('`').lower())
        position=match.end()
    index=0

    def take(expected=None):
        """读取词元；参数：可选期望值；返回：词元。"""
        nonlocal index
        if index>=len(tokens) or expected is not None and tokens[index]!=expected:
            raise ValueError('检查约束结构无法识别')
        value=tokens[index]
        index+=1
        return value

    def peek():
        """查看下一个词元；参数：无；返回：词元或空值。"""
        return tokens[index] if index<len(tokens) else None

    def node(operator,*values):
        """规范化同级结合运算；参数：运算及操作数；返回：树节点。"""
        if operator in ('and','or','+'):
            result=[]
            for value in values:
                result.extend(value[1:] if isinstance(value,tuple) and value[0]==operator else [value])
            return (operator,*result)
        return (operator,*values)

    def parse(minimum=0):
        """按优先级读取表达式；参数：最低优先级；返回：树节点。"""
        token=take()
        if token=='(':
            left=parse()
            take(')')
        elif token in (')',','):
            raise ValueError('检查约束缺少操作数')
        else:
            left=('value',token)
        precedence={'or':1,'and':2,'=':3,'<>':3,'!=':3,'<':3,'>':3,'<=':3,'>=':3,'is':3,'in':3,'not':3,'between':3,'+':4}
        while peek() in precedence and precedence[peek()]>=minimum:
            operator=take()
            rank=precedence[operator]
            if operator=='is':
                negative=peek()=='not'
                if negative:
                    take('not')
                take('null')
                left=node('is not null' if negative else 'is null',left)
            elif operator in ('in','not'):
                if operator=='not':
                    take('in')
                take('(')
                values=[parse(4)]
                while peek()==',':
                    take(',')
                    values.append(parse(4))
                take(')')
                left=node('not in' if operator=='not' else 'in',left,*values)
            elif operator=='between':
                low=parse(4)
                take('and')
                left=node('between',left,low,parse(4))
            else:
                left=node('<>' if operator=='!=' else operator,left,parse(rank+1))
        return left
    result=parse()
    if index!=len(tokens):
        raise ValueError('检查约束存在未解析内容')
    return result
