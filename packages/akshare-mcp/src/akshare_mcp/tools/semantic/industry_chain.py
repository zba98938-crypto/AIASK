"""产业链信息 — get_industry_chain"""

from typing import Optional
from ...utils import ok, fail


# 预置产业链数据
# P2-4.5.3 fix: 扩展到 15+ 主流产业链(诊断报告 §4.5.3)
# 历史问题:仅 5 条产业链覆盖率不足,AI 调用 keyword="新能源汽车"/"军工"/"5G" 等高频场景全部 not_found
_PRESET_CHAINS = [
    {
        'id': 'new_energy',
        'name': '新能源产业链',
        'upstream': ['锂矿', '钴矿', '镍矿', '稀土'],
        'midstream': ['电池材料', '正极材料', '负极材料', '电解液', '隔膜', '电池制造'],
        'downstream': ['新能源汽车', '储能', '充电桩']
    },
    {
        'id': 'semiconductor',
        'name': '半导体产业链',
        'upstream': ['硅片', '光刻胶', '电子特气', '靶材', 'EDA工具'],
        'midstream': ['芯片设计', '晶圆制造', '芯片制造', '光刻设备', '刻蚀设备'],
        'downstream': ['封装测试', '终端应用', '消费电子']
    },
    {
        'id': 'pv',
        'name': '光伏产业链',
        'upstream': ['硅料', '硅片', '银浆', '光伏玻璃'],
        'midstream': ['电池片', '组件', '逆变器'],
        'downstream': ['电站', '运维', '海外出口']
    },
    {
        'id': 'liquor',
        'name': '白酒产业链',
        'upstream': ['高粱', '小麦', '包装', '玻璃瓶', '酒曲'],
        'midstream': ['酿造', '灌装', '高端品牌', '次高端品牌', '区域品牌'],
        'downstream': ['经销渠道', '零售', '电商', '即饮渠道']
    },
    {
        'id': 'pharma',
        'name': '医药产业链',
        'upstream': ['原料药', '中间体', '化学试剂'],
        'midstream': ['制剂', '研发', 'CXO', '医药工业', '生物制品'],
        'downstream': ['流通', '医院', '零售药店', '互联网医疗']
    },
    # P2-4.5.3 新增 — 高频被 AI 询问的产业链
    {
        'id': 'ev_auto',
        'name': '新能源汽车产业链',
        'upstream': ['锂矿', '钴矿', '稀土', '芯片'],
        'midstream': ['动力电池', '电机', '电控', '智能座舱', '自动驾驶'],
        'downstream': ['整车制造', '充电桩', '后市场', '出口']
    },
    {
        'id': 'military',
        'name': '军工产业链',
        'upstream': ['特种金属', '复合材料', '电子元器件'],
        'midstream': ['航空整机', '航天装备', '舰船', '兵器', '雷达'],
        'downstream': ['军方采购', '出口', '军民融合']
    },
    {
        'id': '5g_telecom',
        'name': '5G通信产业链',
        'upstream': ['射频芯片', 'PCB', '光器件', '基带芯片'],
        'midstream': ['基站设备', '光模块', '天线', '小基站'],
        'downstream': ['运营商', '物联网', '车联网', '工业互联网']
    },
    {
        'id': 'consumer_electronics',
        'name': '消费电子产业链',
        'upstream': ['面板', '芯片', '光学元件', '电池'],
        'midstream': ['零部件', '模组', '代工', '组装'],
        'downstream': ['手机', '可穿戴', 'VR/AR', '智能家居']
    },
    {
        'id': 'food_beverage',
        'name': '食品饮料产业链',
        'upstream': ['农产品', '包装材料', '原料添加剂'],
        'midstream': ['加工', '调味品', '乳制品', '休闲食品', '饮料'],
        'downstream': ['商超', '电商', '餐饮', '社区便利店']
    },
    {
        'id': 'real_estate',
        'name': '房地产产业链',
        'upstream': ['水泥', '钢铁', '玻璃', '建材'],
        'midstream': ['开发', '建筑施工', '装修', '物业'],
        'downstream': ['住宅销售', '商业地产', '租赁市场', '保障房']
    },
    {
        'id': 'banking_finance',
        'name': '金融产业链',
        'upstream': ['监管', '征信', '金融科技'],
        'midstream': ['银行', '保险', '证券', '信托'],
        'downstream': ['居民理财', '企业融资', '同业市场', '资本市场']
    },
    {
        'id': 'ai',
        'name': '人工智能产业链',
        'upstream': ['GPU', '大模型芯片', '高端存储', '光通信'],
        'midstream': ['算力中心', '大模型', '算法平台', '云服务'],
        'downstream': ['行业应用', '智能驾驶', '智能客服', '内容生成']
    },
    {
        'id': 'wind_power',
        'name': '风电产业链',
        'upstream': ['碳纤维', '钕铁硼', '风机轴承', '叶片材料'],
        'midstream': ['风机制造', '塔筒', '齿轮箱', '海缆'],
        'downstream': ['陆上风电', '海上风电', '风电运维']
    },
    {
        'id': 'medical_device',
        'name': '医疗器械产业链',
        'upstream': ['原材料', '电子元器件', '生物材料'],
        'midstream': ['影像设备', '体外诊断', '高值耗材', '低值耗材'],
        'downstream': ['公立医院', '民营医院', '基层医疗', '出口']
    },
    {
        'id': 'robotics',
        'name': '机器人产业链',
        'upstream': ['伺服电机', '减速器', '控制器', '传感器'],
        'midstream': ['工业机器人', '协作机器人', '服务机器人', '人形机器人'],
        'downstream': ['汽车制造', '3C电子', '物流仓储', '家庭服务']
    },
    {
        'id': 'energy_storage',
        'name': '储能产业链',
        'upstream': ['锂电池材料', '钠电池材料', 'PCS功率器件'],
        'midstream': ['电芯制造', '储能系统集成', 'BMS'],
        'downstream': ['电源侧储能', '电网侧储能', '用户侧储能', '海外市场']
    },
]


def get_industry_chain(keyword: Optional[str] = None, chain_id: Optional[str] = None):
    """
    获取产业链信息（上中下游环节及关联行业）

    Args:
        keyword (str, optional): 关键词，匹配产业链名称或上中下游环节（如 "锂矿"、"新能源"）
        chain_id (str, optional): 产业链ID（如 "new_energy"、"semiconductor"）

    Returns:
        dict: {"success": bool, "data": {"chains": list[dict], "count": int, "message": str|null}}
        每条 chain 包含: id(str), name(str), upstream(list[str]), midstream(list[str]), downstream(list[str])

    Errors:
        - keyword 和 chain_id 均为空时返回全部预置产业链
        - 未匹配到时先尝试知识图谱扩展，仍无结果则返回全部预置产业链并附提示

    Examples:
        get_industry_chain(keyword="锂矿")
        get_industry_chain(chain_id="semiconductor")
    """
    try:
        chains = _PRESET_CHAINS
        message = None

        alias_map = {
            '酿酒': '白酒',
            '酒类': '白酒',
            '新能源车': '新能源汽车',
            '电动车': '新能源汽车',
            '芯片': '半导体',
            # P2-4.5.3 新增 alias
            '集成电路': '半导体',
            'IC': '半导体',
            '太阳能': '光伏',
            '光伏发电': '光伏',
            '电池': '新能源',
            '锂电': '新能源',
            '储能电池': '储能',
            '汽车': '新能源汽车',
            'EV': '新能源汽车',
            '智能驾驶': '人工智能',
            'AI': '人工智能',
            'GPT': '人工智能',
            '大模型': '人工智能',
            '武器': '军工',
            '国防': '军工',
            '5G': '5G通信',
            '通信': '5G通信',
            '通讯': '5G通信',
            '消费电子': '消费电子',
            '手机': '消费电子',
            '苹果产业链': '消费电子',
            '苹果链': '消费电子',
            '果链': '消费电子',
            '食品': '食品饮料',
            '饮料': '食品饮料',
            '调味品': '食品饮料',
            '乳制品': '食品饮料',
            '地产': '房地产',
            '物业': '房地产',
            '装修': '房地产',
            '银行': '金融',
            '保险': '金融',
            '证券': '金融',
            '券商': '金融',
            '风电': '风电',
            '海上风电': '风电',
            '医疗器械': '医疗器械',
            '医疗设备': '医疗器械',
            '机器人': '机器人',
            '人形机器人': '机器人',
            '工业机器人': '机器人',
            '储能': '储能',
        }

        def _keyword_match(c, kw):
            if not kw:
                return False
            normalized_kw = alias_map.get(kw, kw)
            if normalized_kw != kw and _keyword_match(c, normalized_kw):
                return True
            if kw in c.get('name', ''):
                return True
            for seg in ('upstream', 'midstream', 'downstream'):
                for item in c.get(seg, []):
                    if kw in item:
                        return True
            return False

        if chain_id:
            result = [c for c in chains if c['id'] == chain_id]
        elif keyword:
            kw = (keyword or '').strip()
            result = [c for c in chains if _keyword_match(c, kw)]
            if not result:
                try:
                    from ...services.industry_knowledge_graph import industry_kg
                    kg_res = industry_kg.analyze_chain(kw)
                    if not kg_res.get('error') and kg_res.get('chains'):
                        result = []
                        for i, ch in enumerate(kg_res['chains']):
                            up = [n.get('name', '') for n in ch.get('upstream', [])]
                            down = [n.get('name', '') for n in ch.get('downstream', [])]
                            cur = ch.get('current', {})
                            result.append({
                                'id': f"kg_{i}",
                                'name': cur.get('name', kw) + ' 产业链',
                                'upstream': up,
                                'midstream': [],
                                'downstream': down,
                            })
                except Exception:
                    pass
            if not result:
                # P3-5.13 fix: not_found 显式标 quality_flags(诊断报告 §5.13)
                # 历史问题:silent 返回全部预置 chains,success=true,AI 误以为找到匹配
                result = chains
                message = f'未找到与「{keyword}」匹配的产业链，已返回全部预置产业链（共{len(chains)}条）'
                out = {
                    'chains': result,
                    'count': len(result),
                    'matched': False,
                    'message': message,
                    'quality_flags': ['not_found', 'fallback_to_preset'],
                    'fallback_used': True,
                    'fallback_reason': f'no_match_for_keyword:{keyword}',
                    'requested_keyword': keyword,
                }
                return ok(out)
        else:
            result = chains

        out = {'chains': result, 'count': len(result), 'matched': bool(result and not message)}
        if message:
            out['message'] = message
            out['matched'] = False
        return ok(out)

    except Exception as e:
        return fail(str(e))
