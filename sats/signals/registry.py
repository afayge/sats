from __future__ import annotations

from sats.signals.base import SignalDefinition


SIGNAL_DEFINITIONS: dict[str, SignalDefinition] = {}
SHORT_UP_CATEGORIES = ("ma_kline", "kline_graph", "ma_graph", "graph_graph", "chan", "trendline")


def _add(signal_id: str, label: str, category: str, side: str, description: str = "") -> None:
    SIGNAL_DEFINITIONS[signal_id] = SignalDefinition(signal_id, label, category, side, description)


for item in [
    ("flag_down_break", "降旗形整理向上突破", "graph", "buy"),
    ("flag_up_target", "升旗形达成整理下跌目标", "graph", "sell"),
    ("wedge_down_break", "降楔型整理向上突破", "graph", "buy"),
    ("wedge_up_target", "升楔型达成整理下跌目标", "graph", "sell"),
    ("triple_bottom", "三重底", "graph", "buy"),
    ("triple_top", "三重顶", "graph", "sell"),
    ("head_bottom", "头肩底", "graph", "buy"),
    ("head_top_break", "头肩顶向下突破", "graph", "sell"),
    ("triangle_sym_break", "对称三角形突破", "graph", "buy"),
    ("triangle_up_break", "上升三角整理向上突破", "graph", "buy"),
    ("triangle_down_break", "下降三角形向下突破", "graph", "sell"),
    ("triangle_expand", "扩散三角形", "graph", "hold"),
    ("rect_down_target", "下跌矩形达成下跌目标", "graph", "sell"),
    ("rect_up_break", "矩形整理向上突破", "graph", "buy"),
    ("double_bottom", "双重底", "graph", "buy"),
    ("double_top", "双重顶", "graph", "sell"),
    ("trend_breakthrough_chance", "下降趋势向上突破机会", "trendline", "buy"),
    ("trend_breakthrough_risk", "上升趋势向下破位风险", "trendline", "sell"),
    ("trend_resistance_pullback", "下降趋势临近强阻力回调", "trendline", "sell"),
    ("trend_support_rebound", "上升趋势临近强支撑反弹", "trendline", "buy"),
    ("elliott_c_reversal", "波浪理论回调浪反转", "wave", "buy"),
    ("elliott_b_pullback", "上涨回调浪中继 b", "wave", "hold"),
    ("elliott_c_up_continuation", "波浪理论上涨中继 c", "wave", "buy"),
    ("elliott_c_down_continuation", "波浪理论下跌中继 c", "wave", "sell"),
    ("cypher_bullish", "降赛福 d 点完成", "harmonic", "buy"),
    ("cypher_predict", "c 点将上涨到 d 点完成降赛福", "harmonic", "buy"),
    ("bat_bullish", "蝙蝠到达升蝙蝠 d 点", "harmonic", "buy"),
    ("bat_third_target", "升蝙蝠第 3 反弹目标", "harmonic", "buy"),
    ("gartley_bullish", "降伽利 d 点完成", "harmonic", "buy"),
    ("crab_bullish", "升螃蟹 d 点完成", "harmonic", "buy"),
]:
    _add(*item)

for item in [
    ("ma_granville_b1", "葛兰威尔第①买入点", "ma", "buy"),
    ("ma_granville_b2", "葛兰威尔第②买入点", "ma", "buy"),
    ("ma_granville_b3", "葛兰威尔第③买入点", "ma", "buy"),
    ("ma_granville_b4", "葛兰威尔第④买入点", "ma", "buy"),
    ("ma_granville_s5", "葛兰威尔第⑤卖出点", "ma", "sell"),
    ("ma_granville_s6", "葛兰威尔第⑥卖出点", "ma", "sell"),
    ("ma_granville_s7", "葛兰威尔第⑦卖出点", "ma", "sell"),
    ("ma_granville_s8", "葛兰威尔第⑧卖出点", "ma", "sell"),
    ("ma_alpine_skiing", "高山滑雪", "ma", "buy"),
    ("ma_warplane", "战机起航", "ma", "sell"),
    ("ma_cloud_moon", "烘云托月", "ma", "buy"),
    ("ma_cloud_dark", "乌云密布", "ma", "sell"),
    ("ma_golden_valley", "金山谷", "ma", "buy"),
    ("ma_silver_valley", "银山谷", "ma", "buy"),
    ("ma_dragon_sea", "蛟龙出海", "ma", "buy"),
    ("ma_chopper", "断头铡刀", "ma", "sell"),
    ("ma_golden_spider", "金蜘蛛", "ma", "buy"),
    ("ma_poison_spider", "毒蜘蛛", "ma", "sell"),
    ("ma_dry_up_jump", "旱地拔葱", "ma", "buy"),
    ("ma_dry_dn_jump", "绝命跳", "ma", "sell"),
    ("ma_fish_gate", "鱼跃龙门", "ma", "buy"),
    ("ma_death_valley", "死亡谷", "ma", "sell"),
]:
    _add(*item)

for item in [
    ("kc_up_pioneer", "多方尖兵", "kline", "buy"),
    ("kc_down_pioneer", "空方尖兵", "kline", "sell"),
    ("kc_up_jump_gap", "高开跳空缺口", "kline", "buy"),
    ("kc_down_jump_gap", "跳空下跌缺口", "kline", "sell"),
    ("kc_tower_bottom", "塔形底|圆底", "kline", "buy"),
    ("kc_tower_top", "塔形顶|圆顶", "kline", "sell"),
    ("kc_up_three_methods", "上升三部曲", "kline", "buy"),
    ("kc_down_three_methods", "下降三部曲", "kline", "sell"),
    ("kc_high_five_yin", "高档五阴线", "kline", "sell"),
    ("kc_low_five_yang", "低档五阳线", "kline", "buy"),
    ("kc_up_unbroke", "冉冉上升|稳步上涨", "kline", "buy"),
    ("kc_down_unbroke", "绵绵阴跌|下跌不止", "kline", "sell"),
    ("kc_slow_up", "徐缓上升", "kline", "buy"),
    ("kc_slow_down", "徐缓下降", "kline", "sell"),
    ("kc_down_acceleration", "向下加速度线", "kline", "sell"),
    ("kc_up_acceleration", "向上加速度线", "kline", "buy"),
    ("kc_probe_up", "下探上涨", "kline", "buy"),
    ("kc_probe_down", "上探下跌", "kline", "sell"),
    ("kc_up_resistance", "上升抵抗", "kline", "buy"),
    ("kc_down_resistance", "下跌抵抗", "kline", "sell"),
    ("kc_bullish_cannon", "多方炮", "kline", "buy"),
    ("kc_bearish_cannon", "空方炮", "kline", "sell"),
    ("kc_up_stars", "上涨两颗星|上涨三颗星", "kline", "buy"),
    ("kc_down_stars", "下跌两颗星|下跌三颗星", "kline", "sell"),
    ("kc_down_jump_three_stars", "跳空下跌三颗星", "kline", "sell"),
    ("kc_up_jump_three_stars", "跳空上涨三颗星", "kline", "buy"),
    ("kc_up_cover", "上升覆盖线", "kline", "sell"),
    ("kc_down_cover", "下降覆盖线", "kline", "buy"),
    ("kc_morning_star", "早晨之星", "kline", "buy"),
    ("kc_evening_star", "黄昏之星", "kline", "sell"),
    ("kc_up_pinbar", "上涨Pinbar组合", "kline", "buy"),
    ("kc_down_pinbar", "下跌Pinbar组合", "kline", "sell"),
    ("kc_down_blocked", "降势受阻", "kline", "buy"),
    ("kc_up_blocked", "升势受阻", "kline", "sell"),
    ("kc_down_pause", "降势停顿", "kline", "buy"),
    ("kc_up_pause", "升势停顿", "kline", "sell"),
    ("kc_two_black_one_red", "两黑夹一红", "kline", "sell"),
    ("kc_two_red_one_black", "两红夹一黑", "kline", "buy"),
    ("kc_tweezer_bottom", "上涨镊子线", "kline", "buy"),
    ("kc_tweezer_top", "下跌镊子线", "kline", "sell"),
    ("kc_three_white_soldiers", "红三兵", "kline", "buy"),
    ("kc_three_black_crows", "三只乌鸦", "kline", "sell"),
    ("kc_three_gap_down", "三空阴线", "kline", "sell"),
    ("kc_three_gap_up", "三空阳线", "kline", "buy"),
    ("kc_two_crows", "双飞乌鸦", "kline", "sell"),
    ("kc_down_pour", "倾盆大雨", "kline", "sell"),
    ("kc_sunrise", "旭日东升", "kline", "buy"),
    ("kc_bearish_counterattack", "淡友反攻", "kline", "sell"),
    ("kc_bullish_counterattack", "好友反攻", "kline", "buy"),
    ("kc_shooting_star", "射击之星", "kline", "sell"),
    ("kc_gravestone_doji", "墓碑十字线", "kline", "sell"),
    ("kc_down_spinning_top", "下跌螺旋桨", "kline", "buy"),
    ("kc_up_spinning_top", "上涨螺旋桨", "kline", "sell"),
    ("kc_top_end", "顶部尽头线", "kline", "sell"),
    ("kc_bottom_end", "底部尽头线", "kline", "buy"),
    ("kc_double_needle", "双针探底", "kline", "buy"),
    ("kc_hanging_man", "吊颈线", "kline", "sell"),
    ("kc_dark_cloud", "乌云盖顶", "kline", "sell"),
    ("kc_piercing", "曙光初现", "kline", "buy"),
    ("kc_bullish_harami", "上涨身怀六甲", "kline", "buy"),
    ("kc_bearish_harami", "下跌身怀六甲", "kline", "sell"),
    ("kc_bullish_doji_harami", "上涨孕十字星", "kline", "buy"),
    ("kc_bearish_doji_harami", "下跌孕十字星", "kline", "sell"),
    ("kc_bullish_abandoned_baby", "上涨孤独十字星", "kline", "buy"),
    ("kc_bearish_abandoned_baby", "下跌孤独十字星", "kline", "sell"),
    ("kc_bearish_engulfing", "阴包阳形态", "kline", "sell"),
    ("kc_bullish_engulfing", "阳包阴形态", "kline", "buy"),
    ("kc_low_parallel_yang", "低位并排阳线", "kline", "buy"),
    ("kc_high_parallel_yang", "高位并排阳线", "kline", "sell"),
    ("kc_midstream", "中流砥柱", "kline", "buy"),
    ("kc_single_needle", "单针探底", "kline", "buy"),
    ("kc_hammer", "锤头线", "kline", "buy"),
    ("kc_bearish_inverted_hammer", "看跌倒锤头", "kline", "sell"),
    ("kc_bullish_inverted_hammer", "看涨倒锤头线", "kline", "buy"),
    ("kc_immortal_guide", "仙人指路", "kline", "buy"),
    ("kc_down_insert", "下降插入线", "kline", "sell"),
]:
    _add(*item)

for item in [
    ("chan_first_buy", "一买", "chan", "buy"),
    ("chan_second_buy", "二买", "chan", "buy"),
    ("chan_third_buy", "三买", "chan", "buy"),
    ("chan_second_third_overlap", "二三买重合", "chan", "buy"),
    ("chan_center_oscillation_low", "中枢低吸", "chan", "buy"),
    ("chan_first_sell", "一卖", "chan", "sell"),
    ("chan_second_sell", "二卖", "chan", "sell"),
    ("chan_third_sell", "三卖", "chan", "sell"),
    ("chan_center_oscillation_high", "中枢高抛", "chan", "sell"),
]:
    _add(*item)


COMPOSITE_DEFINITIONS: dict[str, SignalDefinition] = {}


def _comp(signal_id: str, label: str, category: str, side: str) -> None:
    COMPOSITE_DEFINITIONS[signal_id] = SignalDefinition(signal_id, label, category, side)


for item in [
    ("graph_triangle_chan_wave", "上升三角整理向上突破＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_elliott_c_chan", "波浪理论回调浪 c 点＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_trend_break_risk_chan", "升趋势向下破位风险＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "sell"),
    ("graph_cypher_predict_chan", "c 点将上涨到 d 点完成降赛福＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_cypher_bullish_chan", "降赛福 d 点完成＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_chan_third_buy", "缠论中继❸买向上＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_wedge_down_break_chan", "降楔型整理向上突破＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_bat_chan", "蝙蝠到达升蝙蝠 d 点＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_bat_third_target_chan", "升蝙蝠第 3 反弹目标＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_chan_second_buy", "缠论中继❷买向上＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_elliott_reversal_chan", "波浪理论回调浪反转＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_trend_break_chance_chan", "趋势线突破机会＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_chan_third_sell", "缠论中继❸卖向下＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "sell"),
    ("graph_head_top_break_chan", "头肩顶向下突破＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "sell"),
    ("graph_trend_support_break_chan", "升趋势强支撑破位可能＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "sell"),
    ("graph_elliott_b_chan", "上涨回调浪中继 b＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_gartley_chan", "降伽利 d 点完成＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_chan_second_sell", "缠论中继❷卖向下＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "sell"),
    ("graph_rect_down_target_chan", "下跌矩形达成下跌目标＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "sell"),
    ("graph_elliott_down_c_chan", "波浪理论下跌中继 c＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "sell"),
    ("graph_crab_chan", "升螃蟹 d 点完成＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_flag_down_break_chan", "降旗形整理向上突破＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_chan_first_buy", "缠论反转❶买向上＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_elliott_up_c_chan", "波浪理论上涨中继 c＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("graph_wedge_up_target_chan", "升楔型达成整理下跌目标＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "sell"),
    ("graph_chan_center_b_complete", "缠论中枢 b 刚完成＋缠论｜谐波｜趋势线｜波浪｜形态模型", "graph_graph", "buy"),
    ("ma_golden_spider_pattern", "金蜘蛛买入点 ✚ 形态反转中继", "ma_graph", "buy"),
    ("ma_granville_b1_wave", "葛兰威尔第①买入点 ✚ 波浪理论", "ma_graph", "buy"),
    ("ma_golden_silver_valley_chan", "金银山谷买入点 ✚ 缠论", "ma_graph", "buy"),
    ("ma_granville_s6_harmonic", "葛兰威尔第⑥卖出点 ✚ 谐波理论", "ma_graph", "sell"),
    ("ma_warplane_trend", "战机起航卖出点 ✚ 趋势线理论", "ma_graph", "sell"),
    ("ma_granville_b3_chan", "葛兰威尔第③买入点 ✚ 缠论", "ma_graph", "buy"),
    ("ma_cloud_dark_wave", "乌云密布卖出点 ✚ 波浪理论", "ma_graph", "sell"),
    ("ma_dry_up_jump_pattern", "旱地拔葱买入点 ✚ 形态反转中继", "ma_graph", "buy"),
    ("ma_granville_s7_trend", "葛兰威尔第⑦卖出点 ✚ 趋势线理论", "ma_graph", "sell"),
    ("ma_chopper_harmonic", "断头铡刀卖出点 ✚ 谐波理论", "ma_graph", "sell"),
    ("kc_up_pioneer_graph", "多方尖兵＋图形信号", "kline_graph", "buy"),
    ("kc_up_jump_gap_graph", "高开跳空缺口＋图形信号", "kline_graph", "buy"),
    ("kc_tower_bottom_graph", "塔形底|圆底＋图形信号", "kline_graph", "buy"),
    ("kc_up_unbroke_graph", "冉冉上升|稳步上涨＋图形信号", "kline_graph", "buy"),
    ("kc_down_blocked_graph", "降势受阻＋图形信号", "kline_graph", "buy"),
    ("kc_up_pinbar_graph", "上涨Pinbar组合＋图形信号", "kline_graph", "buy"),
    ("kc_down_pour_graph", "倾盆大雨＋图形信号", "kline_graph", "sell"),
    ("kc_low_five_yang_graph", "低档五阳线＋图形信号", "kline_graph", "buy"),
    ("kc_bullish_harami_graph", "上涨身怀六甲＋图形信号", "kline_graph", "buy"),
    ("kc_double_needle_graph", "双针探底＋图形信号", "kline_graph", "buy"),
    ("ma_b2_kline", "葛兰威尔第②买入点 ✚ K线信号", "ma_kline", "buy"),
    ("ma_poison_spider_kline", "毒蜘蛛卖出点 ✚ K线信号", "ma_kline", "sell"),
    ("ma_fish_gate_kline", "鱼跃龙门买入点 ✚ K线信号", "ma_kline", "buy"),
    ("ma_death_valley_kline", "死亡谷卖出点 ✚ K线信号", "ma_kline", "sell"),
    ("ma_alpine_skiing_kline", "高山滑雪买入点 ✚ K线信号", "ma_kline", "buy"),
    ("ma_b4_kline", "葛兰威尔第④买入点 ✚ K线信号", "ma_kline", "buy"),
    ("ma_cloud_moon_kline", "烘云托月买入点 ✚ K线信号", "ma_kline", "buy"),
    ("ma_dry_dn_jump_kline", "绝命跳卖出点 ✚ K线信号", "ma_kline", "sell"),
    ("ma_b3_kline", "葛兰威尔第③买入点 ✚ K线信号", "ma_kline", "buy"),
    ("ma_dragon_sea_kline", "蛟龙出海买入点 ✚ K线信号", "ma_kline", "buy"),
]:
    _comp(*item)


GROUP_ALIASES = {
    "all": (
        "graph",
        "trendline",
        "ma",
        "kline",
        "wave",
        "harmonic",
        "chan",
        "graph_graph",
        "ma_graph",
        "kline_graph",
        "ma_kline",
    ),
    "graph": ("graph",),
    "pattern": ("graph",),
    "trendline": ("trendline",),
    "ma": ("ma",),
    "kline": ("kline",),
    "kc": ("kline",),
    "wave": ("wave",),
    "elliott": ("wave",),
    "harmonic": ("harmonic",),
    "chan": ("chan",),
    "graph_graph": ("graph_graph",),
    "ma_graph": ("ma_graph",),
    "kline_graph": ("kline_graph",),
    "ma_kline": ("ma_kline",),
    "short_up": SHORT_UP_CATEGORIES,
}


def list_signal_definitions(*, category: str | None = None) -> list[SignalDefinition]:
    items = [*SIGNAL_DEFINITIONS.values(), *COMPOSITE_DEFINITIONS.values()]
    if category:
        if category == "short_up":
            wanted = set(SHORT_UP_CATEGORIES)
            items = [item for item in items if item.category in wanted and item.side == "buy"]
            return sorted(items, key=lambda item: (item.category, item.signal_id))
        wanted = set(GROUP_ALIASES.get(category, (category,)))
        items = [item for item in items if item.category in wanted]
    return sorted(items, key=lambda item: (item.category, item.signal_id))


def get_signal_definition(signal_id: str) -> SignalDefinition | None:
    return SIGNAL_DEFINITIONS.get(signal_id) or COMPOSITE_DEFINITIONS.get(signal_id)
