"""RAG 领域词表：查询分类、拆分与改写的数据文件。

为什么是数据不是代码
--------------------
分类与改写的质量取决于词表覆盖率，把词表从逻辑里拆出来，
运营/开发者可以不改代码直接扩词表。词表按"最大命中优先"匹配，
新增词条不需要重新训练任何东西。

词表来源：与指标字典（metrics/registry.py）和知识库语料（mock/kb_docs.py）
保持同源——词典里出现的词必须是系统真的能回答的，避免"分类对了但查无此物"。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 指标意图词表：surface → 指标代码（与 metrics/registry.DEFINITIONS 同源）
# ---------------------------------------------------------------------------

METRIC_TERMS: tuple[tuple[str, str], ...] = (
    ("下单GMV", "GMV_ORDER"), ("下单金额", "GMV_ORDER"), ("下单gmv", "GMV_ORDER"),
    ("支付GMV", "GMV_PAID"), ("支付金额", "GMV_PAID"), ("支付gmv", "GMV_PAID"),
    ("成交金额", "GMV_PAID"), ("成交额", "GMV_PAID"), ("gmv", "GMV_PAID"),
    ("结算GMV", "GMV_SETTLE"), ("结算金额", "GMV_SETTLE"), ("对账金额", "GMV_SETTLE"),
    ("退款金额", "REFUND_AMOUNT"), ("退款额", "REFUND_AMOUNT"),
    ("退款率", "REFUND_RATE"),
    ("订单数", "ORDER_CNT"), ("订单量", "ORDER_CNT"),
    ("支付订单数", "PAID_CNT"),
    ("客单价", "AOV_PAID"),
    ("会话量", "CS_SESSION_CNT"), ("咨询量", "CS_SESSION_CNT"),
    ("机器人接待", "CS_BOT_RATIO"), ("机器人占比", "CS_BOT_RATIO"),
    ("首响", "CS_FIRST_RESP_AVG"), ("响应时长", "CS_FIRST_RESP_AVG"),
    ("满意度", "CS_SATISFACTION_AVG"),
    ("解决率", "CS_RESOLVE_RATE"), ("一次解决", "CS_RESOLVE_RATE"),
)

# ---------------------------------------------------------------------------
# 知识意图词表：surface → kb_type 提示（与 mock/kb_docs.py 语料同源）
# ---------------------------------------------------------------------------

KB_DOMAIN_TERMS: tuple[tuple[str, str], ...] = (
    # policy 域
    ("退货政策", "policy"), ("退货", "policy"), ("退款政策", "policy"),
    ("换货", "policy"), ("换码", "policy"), ("价保", "policy"),
    ("发票", "policy"), ("开票", "policy"), ("红冲", "policy"),
    ("会员", "policy"), ("积分", "policy"), ("尺码", "policy"),
    ("维修", "policy"), ("保修", "policy"), ("售后", "policy"),
    ("发货时效", "policy"), ("发货", "policy"),
    # cs_faq 域（口语化问句特征）
    ("运费", "cs_faq"), ("快递", "cs_faq"), ("物流", "cs_faq"),
    ("收货地址", "cs_faq"), ("改地址", "cs_faq"), ("货到付款", "cs_faq"),
    ("色差", "cs_faq"), ("人工客服", "cs_faq"), ("断货", "cs_faq"),
    ("包装", "cs_faq"), ("到账", "cs_faq"), ("退款多久", "cs_faq"),
    # sop 域
    ("首响", "sop"), ("话术", "sop"), ("投诉", "sop"), ("安抚", "sop"),
    ("质检", "sop"), ("知识库维护", "sop"), ("规范", "sop"),
    # product 域
    ("面料", "product"), ("材质", "product"), ("洗涤", "product"),
    ("规格", "product"), ("工艺", "product"), ("配比", "product"),
)

# 复合句连接词：出现即尝试按其拆分子查询
CONNECTORS: tuple[str, ...] = ("另外", "还有", "以及", "顺便", "再问下", "再问",
                               "；", ";", "？", "?", "。")

# 时间词：会话指代消解时做槽位继承
TEMPORAL_TERMS: tuple[str, ...] = ("上个月", "上周", "本周", "这个月", "近7天",
                                   "近30天", "近90天", "昨天", "今天")

# 口语 → 标准词的改写同义词表（无 LLM 时唯一可用的"语义"扩展）
# 键是口语表述，值是标准语料词（必须能在 kb_docs 语料里找到）
SYNONYMS: tuple[tuple[str, str], ...] = (
    ("不想要了", "无理由退货 退货政策"),
    ("后悔了", "无理由退货 退货政策"),
    ("买错了", "换货 换货政策"),
    ("尺码不合适", "换货 尺码对照表"),
    ("买大了", "换码 尺码对照表"),
    ("买小了", "换码 尺码对照表"),
    ("钱什么时候回来", "退款多久到账 原路退回"),
    ("钱退回来", "退款多久到账 原路退回"),
    ("什么时候能到", "发货时效 物流"),
    ("多久发货", "发货时效 48 小时"),
    ("多快发货", "发货时效 48 小时"),
    ("能便宜点吗", "价保 优惠"),
    ("降价了", "价保 差额"),
    ("怎么开票", "发票 电子普票"),
    ("开票", "发票"),
    ("坏了我怎么办", "售后与维修 免费维修"),
    ("坏了", "维修 保修"),
    ("进水了", "保修 有偿维修"),
    ("不满意", "投诉 安抚"),
    ("服务态度差", "投诉 安抚话术"),
    ("积分怎么用", "会员 积分抵扣"),
    ("会员有什么用", "会员权益 积分"),
    ("怎么转人工", "人工客服 转接"),
    ("真人客服", "人工客服 转接"),
    ("会掉色吗", "色差 洗涤"),
    ("有色差吗", "色差"),
    ("原装包装", "原包装 防拆封条"),
    ("货到付", "货到付款"),
    ("改地址", "收货地址 修改"),
    ("断货了", "到货提醒 补货"),
    ("没货了", "断货 到货提醒"),
)


def lookup_metric_terms(text: str) -> list[str]:
    """返回文本命中的指标代码（去重、按词表顺序）。"""
    found: list[str] = []
    lowered = text.lower()
    for surface, code in METRIC_TERMS:
        if surface in text or surface in lowered:
            if code not in found:
                found.append(code)
    return found


def lookup_kb_domains(text: str) -> list[str]:
    """返回文本命中的知识域提示（去重）。"""
    found: list[str] = []
    for surface, domain in KB_DOMAIN_TERMS:
        if surface in text and domain not in found:
            found.append(domain)
    return found


def lookup_synonyms(text: str) -> list[str]:
    """返回文本命中的同义扩展短语（用于改写）。"""
    expansions: list[str] = []
    for colloquial, standard in SYNONYMS:
        if colloquial in text:
            expansions.append(standard)
    return expansions
