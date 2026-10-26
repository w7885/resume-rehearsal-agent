"""Section-specific editing rules; formatting never requires project outcomes."""
from app.resume_structure import heading, SECTION_TYPES

EXPERIENCE_KINDS = {'project', 'work'}
LABELS = {'profile': '基本信息', 'education': '教育背景', 'skills': '专业技能',
          'summary': '自我评价与优势', 'awards': '获奖与证书'}
RULES = {
    'profile': '整理已有字段的顺序、标题、分隔与Markdown排版。姓名、联系方式、求职意向等事实沿用原文，不推断缺项；不要重复索取已有号码。信息清楚时直接给排版建议或说明保留，无需职责、动作、效果采访。布局偏好用preference，缺少必要字段才用details询问。',
    'education': '规范学校、专业、学历、日期、课程等表达。已有信息足够时直接整理；必要缺项才问details，不问职责或上线效果，不猜学历、成绩和毕业日期。',
    'skills': '按实际技能与目标岗位整理分组和表达，可引用本人的项目材料佐证。不能把了解升级为精通；需要补充时问使用过的具体工具或场景（details），不套用项目效果门槛。',
    'summary': '完善自我评价、个人优势、个人简介。已有具体材料时可提炼为简洁、克制的行为表达，也可引用同人当前有效项目材料作为依据。空泛优势缺支持时用strength问一个具体例子；允许用户只说明偏好或工作方式。不能凭空增加领导力、资质或业绩，不追问上线效果，不机械把项目访谈套过来。原文已有的自述可保守润色，不升级为绝对评价。',
    'awards': '规范实际奖项、证书名称、时间和层级。缺少必要事实才问details，不猜证书或奖项；不要求业务效果。',
}


def section_title(markdown, kind):
    for line in markdown.splitlines():
        label = heading(line).split('：', 1)[0].split(':', 1)[0].strip()
        if SECTION_TYPES.get(label.lower()) == kind:
            return label
    return LABELS.get(kind, '当前段落')


def question(doc):
    label = section_title(doc.raw_text, doc.kind)
    if doc.kind == 'summary':
        return ('你最想在这段突出哪项优势？可选一件亲身经历，按“当时的情况、你的做法”说起；没有具体例子也可以先说工作偏好。', 'strength')
    prompts = {
        'profile': '基本信息中，你希望保留哪些展示字段？可先选联系方式或求职意向中的一项；已有内容不用重新填写。',
        'education': '教育信息中，哪一项需要补充或更正？可先说学校、专业或时间中的一项；原文准确的不用重复。',
        'skills': '你希望补充哪项实际使用过的技能？可说一次具体使用场景和你完成的任务；只了解的也可以照实说。',
        'awards': '这段奖项或证书中，哪一项信息需要补充？可先说正式名称、时间或级别中的一项；不确定的可以暂留空。',
    }
    return prompts.get(doc.kind, f'{label}中，你最想调整什么？先说一项即可。'), 'details'
