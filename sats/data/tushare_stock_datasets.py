from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TushareStockDataset:
    dataset: str
    api: str
    title: str
    domain: str
    category: str
    doc_id: int
    min_points: int | None
    permission_status: str
    input_fields: tuple[str, ...]
    output_fields: tuple[str, ...]
    default_fields: tuple[str, ...]
    status: str = "active"
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "api": self.api,
            "title": self.title,
            "domain": self.domain,
            "category": self.category,
            "doc_id": self.doc_id,
            "min_points": self.min_points,
            "permission_status": self.permission_status,
            "input_fields": list(self.input_fields),
            "output_fields": list(self.output_fields),
            "default_fields": list(self.default_fields),
            "status": self.status,
            "tags": list(self.tags),
        }


def _fields(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _permission_status(min_points: int | None, status: str) -> str:
    if status == "deprecated":
        return "included_deprecated"
    if min_points is None:
        return "included_unscored"
    if min_points == 0:
        return "included_no_point_label"
    return f"included_{min_points}_points"


def _ds(
    dataset: str,
    title: str,
    category: str,
    doc_id: int,
    min_points: int | None,
    inputs: str,
    outputs: str,
    *,
    domain: str = "股票数据",
    status: str = "active",
    tags: str = "",
    default_fields: str | None = None,
) -> TushareStockDataset:
    output_fields = _fields(outputs)
    selected_defaults = _fields(default_fields) if default_fields is not None else output_fields[:12]
    return TushareStockDataset(
        dataset=dataset,
        api=dataset,
        title=title,
        domain=domain,
        category=category,
        doc_id=doc_id,
        min_points=min_points,
        permission_status=_permission_status(min_points, status),
        input_fields=_fields(inputs),
        output_fields=output_fields,
        default_fields=selected_defaults,
        status=status,
        tags=_fields(tags),
    )


TUSHARE_STOCK_DATASETS: dict[str, TushareStockDataset] = {
    item.dataset: item
    for item in [
        _ds("stock_basic", "股票列表", "基础数据", 25, 2000, "ts_code,name,market,list_status,exchange,is_hs", "ts_code,symbol,name,area,industry,fullname,market,exchange,list_status,list_date,delist_date,is_hs"),
        _ds("trade_cal", "交易日历", "基础数据", 26, 2000, "exchange,start_date,end_date,is_open", "exchange,cal_date,is_open,pretrade_date"),
        _ds("stock_st", "ST股票列表", "基础数据", 397, 3000, "ts_code,trade_date,start_date,end_date", "ts_code,name,trade_date,type,type_name"),
        _ds("st", "ST风险警示板股票", "基础数据", 423, 6000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,name,type,type_name"),
        _ds("stock_hsgt", "沪深港通股票列表", "基础数据", 398, 3000, "ts_code,trade_date,type,start_date,end_date", "ts_code,trade_date,type,name,type_name"),
        _ds("namechange", "股票曾用名", "基础数据", 100, 0, "ts_code,start_date,end_date", "ts_code,name,start_date,end_date,ann_date,change_reason"),
        _ds("stock_company", "上市公司基本信息", "基础数据", 112, 120, "ts_code,exchange", "ts_code,com_name,com_id,exchange,chairman,manager,secretary,reg_capital,setup_date,province,city,main_business"),
        _ds("stk_managers", "上市公司管理层", "基础数据", 193, 2000, "ts_code,ann_date,start_date,end_date", "ts_code,ann_date,name,gender,lev,title,edu,national,birthday,begin_date,end_date,resume"),
        _ds("stk_rewards", "管理层薪酬和持股", "基础数据", 194, 2000, "ts_code,end_date", "ts_code,ann_date,end_date,name,title,reward,hold_vol"),
        _ds("bse_mapping", "北交所新旧代码对照", "基础数据", 375, 120, "o_code,n_code", "name,o_code,n_code,list_date"),
        _ds("new_share", "IPO新股上市", "基础数据", 123, 120, "start_date,end_date", "ts_code,sub_code,name,ipo_date,issue_date,amount,market_amount,price,pe,limit_amount,funds,ballot"),
        _ds("bak_basic", "股票历史列表", "基础数据", 262, 5000, "trade_date,ts_code", "trade_date,ts_code,name,industry,area,pe,float_share,total_share,total_assets,pb,list_date,holder_num"),
        _ds("daily", "历史日线", "行情数据", 27, 0, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"),
        _ds("weekly", "周线行情", "行情数据", 144, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"),
        _ds("monthly", "月线行情", "行情数据", 145, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"),
        _ds("stk_weekly_monthly", "周/月线行情(每日更新)", "行情数据", 336, 2000, "ts_code,trade_date,start_date,end_date,freq", "ts_code,trade_date,end_date,freq,open,high,low,close,pre_close,vol,amount,pct_chg"),
        _ds("stk_week_month_adj", "周/月线复权行情(每日更新)", "行情数据", 365, 2000, "ts_code,trade_date,start_date,end_date,freq", "ts_code,trade_date,end_date,freq,open,high,low,close,open_qfq,close_qfq,open_hfq,close_hfq,vol,amount,pct_chg"),
        _ds("adj_factor", "复权因子", "行情数据", 28, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,adj_factor"),
        _ds("daily_basic", "每日指标", "行情数据", 32, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,pb,ps,total_share,float_share,free_share,total_mv,circ_mv"),
        _ds("stk_limit", "每日涨跌停价格", "行情数据", 183, 2000, "ts_code,trade_date,start_date,end_date", "trade_date,ts_code,pre_close,up_limit,down_limit"),
        _ds("suspend_d", "每日停复牌信息", "行情数据", 214, 0, "ts_code,trade_date,start_date,end_date,suspend_type", "ts_code,trade_date,suspend_timing,suspend_type"),
        _ds("hsgt_top10", "沪深股通十大成交股", "行情数据", 48, 0, "ts_code,trade_date,start_date,end_date,market_type", "trade_date,ts_code,name,close,change,rank,market_type,amount,net_amount,buy,sell"),
        _ds("ggt_top10", "港股通十大成交股", "行情数据", 49, 0, "ts_code,trade_date,start_date,end_date,market_type", "trade_date,ts_code,name,close,p_change,rank,market_type,amount,net_amount,sh_amount,sz_amount"),
        _ds("ggt_daily", "港股通每日成交统计", "行情数据", 196, 2000, "trade_date,start_date,end_date", "trade_date,buy_amount,buy_volume,sell_amount,sell_volume"),
        _ds("ggt_monthly", "港股通每月成交统计", "行情数据", 197, 5000, "month,start_month,end_month", "month,day_buy_amt,day_buy_vol,day_sell_amt,day_sell_vol,total_buy_amt,total_buy_vol,total_sell_amt,total_sell_vol"),
        _ds("bak_daily", "备用行情", "行情数据", 255, 5000, "ts_code,trade_date,start_date,end_date,offset,limit", "ts_code,trade_date,name,pct_change,close,change,open,high,low,pre_close,vol,amount,total_mv,float_mv,pe,industry"),
        _ds("income", "利润表", "财务数据", 33, 2000, "ts_code,ann_date,f_ann_date,start_date,end_date,period,report_type,comp_type", "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,basic_eps,total_revenue,revenue,oper_cost,operate_profit,total_profit,n_income,ebit,ebitda,update_flag"),
        _ds("balancesheet", "资产负债表", "财务数据", 36, 2000, "ts_code,ann_date,start_date,end_date,period,report_type,comp_type", "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,total_share,total_assets,total_cur_assets,total_liab,total_cur_liab,total_hldr_eqy_inc_min_int,update_flag"),
        _ds("cashflow", "现金流量表", "财务数据", 44, 2000, "ts_code,ann_date,f_ann_date,start_date,end_date,period,report_type,comp_type,is_calc", "ts_code,ann_date,f_ann_date,end_date,comp_type,report_type,net_profit,c_inf_fr_operate_a,n_cashflow_act,n_cashflow_inv_act,n_cash_flows_fnc_act,free_cashflow,update_flag"),
        _ds("forecast", "业绩预告", "财务数据", 45, 2000, "ts_code,ann_date,start_date,end_date,period,type", "ts_code,ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,net_profit_max,last_parent_net,first_ann_date,summary,change_reason"),
        _ds("express", "业绩快报", "财务数据", 46, 2000, "ts_code,ann_date,start_date,end_date,period", "ts_code,ann_date,end_date,revenue,operate_profit,total_profit,n_income,total_assets,diluted_eps,diluted_roe,yoy_net_profit,perf_summary"),
        _ds("dividend", "分红送股数据", "财务数据", 103, 2000, "ts_code,ann_date,record_date,ex_date,imp_ann_date", "ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,cash_div,cash_div_tax,record_date,ex_date,pay_date"),
        _ds("fina_indicator", "财务指标数据", "财务数据", 79, 2000, "ts_code,ann_date,start_date,end_date,period", "ts_code,ann_date,end_date,eps,dt_eps,gross_margin,current_ratio,quick_ratio,roe,roa,roic,debt_to_assets,netprofit_margin,grossprofit_margin,rd_exp,update_flag"),
        _ds("fina_audit", "财务审计意见", "财务数据", 80, 2000, "ts_code,ann_date,start_date,end_date,period", "ts_code,ann_date,end_date,audit_result,audit_fees,audit_agency,audit_sign"),
        _ds("fina_mainbz", "主营业务构成", "财务数据", 81, 2000, "ts_code,period,type,start_date,end_date", "ts_code,end_date,bz_item,bz_code,bz_sales,bz_profit,bz_cost,curr_type,update_flag"),
        _ds("disclosure_date", "财报披露日期表", "财务数据", 162, 500, "ts_code,end_date,pre_date,ann_date,actual_date", "ts_code,ann_date,end_date,pre_date,actual_date,modify_date"),
        _ds("top10_holders", "前十大股东", "参考数据", 61, 2000, "ts_code,period,ann_date,start_date,end_date", "ts_code,ann_date,end_date,holder_name,hold_amount,hold_ratio,hold_float_ratio,hold_change,holder_type"),
        _ds("top10_floatholders", "前十大流通股东", "参考数据", 62, 2000, "ts_code,period,ann_date,start_date,end_date", "ts_code,ann_date,end_date,holder_name,hold_amount,hold_ratio,hold_float_ratio,hold_change,holder_type"),
        _ds("pledge_stat", "股权质押统计数据", "参考数据", 110, 2000, "ts_code,end_date", "ts_code,end_date,pledge_count,unrest_pledge,rest_pledge,total_share,pledge_ratio"),
        _ds("pledge_detail", "股权质押明细数据", "参考数据", 111, 500, "ts_code,ann_date,start_date,end_date", "ts_code,ann_date,holder_name,pledge_amount,start_date,end_date,is_release,release_date,pledgor,holding_amount,pledged_amount,p_total_ratio"),
        _ds("repurchase", "股票回购", "参考数据", 124, 600, "ann_date,start_date,end_date", "ts_code,ann_date,end_date,proc,exp_date,vol,amount,high_limit,low_limit"),
        _ds("share_float", "限售股解禁", "参考数据", 160, 120, "ts_code,ann_date,float_date,start_date,end_date", "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type"),
        _ds("block_trade", "大宗交易", "参考数据", 161, 300, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,price,vol,amount,buyer,seller"),
        _ds("stk_account", "股票开户数据（停）", "参考数据", 164, 600, "date,start_date,end_date", "date,weekly_new,total,weekly_hold,weekly_trade", status="deprecated"),
        _ds("stk_account_old", "股票开户数据（旧）", "参考数据", 165, 600, "start_date,end_date", "date,new_sh,new_sz,active_sh,active_sz,total_sh,total_sz,trade_sh,trade_sz"),
        _ds("stk_holdernumber", "股东人数", "参考数据", 166, 600, "ts_code,ann_date,enddate,start_date,end_date", "ts_code,ann_date,end_date,holder_num"),
        _ds("stk_holdertrade", "股东增减持", "参考数据", 175, 2000, "ts_code,ann_date,start_date,end_date,trade_type,holder_type", "ts_code,ann_date,holder_name,holder_type,in_de,change_vol,change_ratio,after_share,after_ratio,avg_price,total_share,begin_date,close_date"),
        _ds("cyq_perf", "每日筹码及胜率", "特色数据", 293, 5000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,his_low,his_high,cost_5pct,cost_15pct,cost_50pct,cost_85pct,cost_95pct,weight_avg,winner_rate"),
        _ds("cyq_chips", "每日筹码分布", "特色数据", 294, 5000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,price,percent"),
        _ds("stk_factor_pro", "股票技术面因子(专业版）", "特色数据", 328, 5000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,open,high,low,close,pct_chg,vol,amount,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv,adj_factor,macd_bfq,rsi_bfq_6"),
        _ds("ccass_hold", "中央结算系统持股统计", "特色数据", 295, 120, "ts_code,hk_code,trade_date,start_date,end_date", "trade_date,ts_code,name,shareholding,hold_nums,hold_ratio"),
        _ds("hk_hold", "沪深股通持股明细", "特色数据", 188, 120, "code,ts_code,trade_date,start_date,end_date,exchange", "code,trade_date,ts_code,name,vol,ratio,exchange"),
        _ds("stk_ah_comparison", "AH股比价", "特色数据", 399, 5000, "hk_code,ts_code,trade_date,start_date,end_date", "hk_code,ts_code,trade_date,hk_name,hk_pct_chg,hk_close,name,close,pct_chg,ah_comparison,ah_premium"),
        _ds("stk_surv", "机构调研数据", "特色数据", 275, 5000, "ts_code,trade_date,start_date,end_date", "ts_code,name,surv_date,fund_visitors,rece_place,rece_mode,rece_org,org_type,comp_rece,content"),
        _ds("stk_nineturn", "神奇九转指标", "特色数据", 364, 6000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,name,close,high,low,turnover_rate,up_count,down_count,nine_up,nine_down"),
        _ds("broker_recommend", "券商月度金股", "特色数据", 267, 6000, "month,broker,ts_code", "month,broker,ts_code,name,rank,weight,price,summary"),
        _ds("stk_shock", "个股异常波动", "特色数据", 451, 6000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,name,trade_market,reason,period"),
        _ds("stk_high_shock", "个股严重异常波动", "特色数据", 452, 6000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,name,trade_market,reason,period"),
        _ds("stk_alert", "股票异动预警", "特色数据", 453, 6000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,name,alert_type,alert_msg"),
        _ds("margin", "融资融券交易汇总", "两融及转融通", 58, 2000, "trade_date,start_date,end_date,exchange_id", "trade_date,exchange_id,rzye,rzmre,rzche,rqye,rqmcl,rzrqye,rqyl"),
        _ds("margin_detail", "融资融券交易明细", "两融及转融通", 59, 2000, "trade_date,ts_code,start_date,end_date", "trade_date,ts_code,name,rzye,rqye,rzmre,rqyl,rzche,rqchl,rqmcl,rzrqye"),
        _ds("margin_secs", "融资融券标的（盘前）", "两融及转融通", 326, 2000, "ts_code,trade_date,exchange,start_date,end_date", "trade_date,ts_code,name,exchange"),
        _ds("slb_sec", "转融券交易汇总(停）", "两融及转融通", 332, 2000, "trade_date,ts_code,start_date,end_date", "trade_date,ts_code,name,ope_inv,lent_qnt,cls_inv,end_bal", status="deprecated"),
        _ds("slb_len", "转融资交易汇总", "两融及转融通", 331, 2000, "trade_date,start_date,end_date", "trade_date,ob,auc_amount,repo_amount,repay_amount,cb"),
        _ds("slb_sec_detail", "转融券交易明细(停）", "两融及转融通", 333, 2000, "trade_date,ts_code,start_date,end_date", "trade_date,ts_code,name,tenor,fee_rate,lent_qnt", status="deprecated"),
        _ds("slb_len_mm", "做市借券交易汇总(停）", "两融及转融通", 334, 2000, "trade_date,ts_code,start_date,end_date", "trade_date,ts_code,name,ope_inv,lent_qnt,cls_inv,end_bal", status="deprecated"),
        _ds("moneyflow", "个股资金流向", "资金流向数据", 170, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,buy_sm_vol,buy_sm_amount,sell_sm_vol,sell_sm_amount,buy_md_vol,buy_md_amount,buy_lg_vol,buy_lg_amount,buy_elg_vol,buy_elg_amount,net_mf_amount"),
        _ds("moneyflow_dc", "个股资金流向（DC）", "资金流向数据", 349, 5000, "ts_code,trade_date,start_date,end_date", "trade_date,ts_code,name,pct_change,close,net_amount,net_amount_rate,buy_elg_amount,buy_lg_amount,buy_md_amount,buy_sm_amount"),
        _ds("moneyflow_hsgt", "沪深港通资金流向", "资金流向数据", 47, 2000, "trade_date,start_date,end_date", "trade_date,ggt_ss,ggt_sz,hgt,sgt,north_money,south_money"),
        _ds("moneyflow_ths", "个股资金流向（THS）", "资金流向数据", 348, 6000, "ts_code,trade_date,start_date,end_date", "trade_date,ts_code,name,pct_change,latest,net_amount,net_d5_amount,buy_lg_amount,buy_elg_amount"),
        _ds("moneyflow_ind_ths", "行业资金流向（THS）", "资金流向数据", 343, 6000, "trade_date,start_date,end_date,ts_code", "trade_date,ts_code,name,pct_change,latest,net_amount,net_d5_amount,buy_lg_amount,buy_elg_amount"),
        _ds("moneyflow_ind_dc", "板块资金流向（DC）", "资金流向数据", 344, 6000, "trade_date,start_date,end_date,ts_code,content_type", "trade_date,ts_code,name,pct_change,close,net_amount,net_amount_rate,buy_elg_amount,buy_lg_amount,buy_md_amount,buy_sm_amount"),
        _ds("moneyflow_mkt_dc", "大盘资金流向（DC）", "资金流向数据", 345, 6000, "trade_date,start_date,end_date", "trade_date,close,pct_change,net_amount,net_amount_rate,buy_elg_amount,buy_lg_amount,buy_md_amount,buy_sm_amount"),
        _ds("moneyflow_cnt_ths", "板块资金流向（THS）", "资金流向数据", 371, 6000, "ts_code,trade_date,start_date,end_date", "trade_date,ts_code,name,pct_change,latest,net_amount,net_d5_amount,buy_lg_amount,buy_elg_amount"),
        _ds("top_list", "龙虎榜每日统计单", "打板专题数据", 106, 2000, "trade_date,ts_code", "trade_date,ts_code,name,close,pct_change,turnover_rate,amount,l_sell,l_buy,l_amount,net_amount,net_rate,reason"),
        _ds("top_inst", "龙虎榜机构交易单", "打板专题数据", 107, 5000, "trade_date,ts_code", "trade_date,ts_code,exalter,side,buy,buy_rate,sell,sell_rate,net_buy,reason"),
        _ds("limit_list_d", "涨跌停和炸板数据", "打板专题数据", 298, 5000, "trade_date,ts_code,limit_type,exchange,start_date,end_date", "trade_date,ts_code,industry,name,close,pct_chg,amount,limit_amount,float_mv,total_mv,turnover_ratio,fd_amount,first_time,last_time,open_times,limit"),
        _ds("hm_list", "市场游资最全名录", "打板专题数据", 311, 5000, "name", "name,desc,orgs"),
        _ds("kpl_list", "榜单数据（KP）", "打板专题数据", 347, 5000, "ts_code,trade_date,tag,start_date,end_date", "ts_code,name,trade_date,lu_time,ld_time,open_time,last_time,lu_desc,tag,theme,pct_chg,amount,status"),
        _ds("kpl_concept_cons", "题材成分（KP）", "打板专题数据", 351, 5000, "trade_date,ts_code,con_code", "ts_code,name,con_name,con_code,trade_date,desc,hot_num"),
        _ds("ths_index", "同花顺行业概念板块", "打板专题数据", 259, 6000, "ts_code,exchange,type", "ts_code,name,count,exchange,list_date,type"),
        _ds("ths_daily", "同花顺概念和行业指数行情", "打板专题数据", 260, 6000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,close,open,high,low,pre_close,avg_price,change,pct_change,vol,turnover_rate"),
        _ds("ths_member", "同花顺行业概念成分", "打板专题数据", 261, 6000, "ts_code,con_code,is_new", "ts_code,code,name,weight,in_date,out_date,is_new"),
        _ds("dc_index", "东方财富概念板块", "打板专题数据", 362, 6000, "ts_code,trade_date,start_date,end_date", "trade_date,ts_code,name,leader,name_list,chg_pct,close,num,up_num"),
        _ds("dc_member", "东方财富概念成分", "打板专题数据", 363, 6000, "ts_code,trade_date,start_date,end_date,con_code", "trade_date,ts_code,con_code,con_name,name,close,pct_change"),
        _ds("dc_daily", "东财概念和行业指数行情", "打板专题数据", 382, 6000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,name,close,open,high,low,pre_close,pct_change,vol,amount"),
        _ds("tdx_index", "通达信板块信息", "打板专题数据", 376, 6000, "ts_code,exchange,type", "ts_code,name,count,exchange,type,list_date"),
        _ds("tdx_member", "通达信板块成分", "打板专题数据", 377, 6000, "ts_code,con_code", "ts_code,code,name,weight,in_date,out_date"),
        _ds("tdx_daily", "通达信板块行情", "打板专题数据", 378, 6000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,name,close,open,high,low,pre_close,pct_change,vol,amount"),
        _ds("ths_hot", "同花顺App热榜数据", "打板专题数据", 320, 6000, "trade_date,ts_code,market,type,is_new", "trade_date,ts_code,name,market,type,rank,pct_change,current_price,concept,rank_reason"),
    ]
}

TUSHARE_COMMON_DATASETS: dict[str, TushareStockDataset] = {
    item.dataset: item
    for item in [
        _ds("etf_basic", "ETF基本信息", "ETF专题", 385, 6000, "ts_code,market,status", "ts_code,name,management,custodian,category,issue_date,list_date,market,status", domain="ETF专题", tags="etf,fund"),
        _ds("etf_index", "ETF基准指数", "ETF专题", 386, 6000, "ts_code,index_code", "ts_code,index_code,index_name,name,market,list_date", domain="ETF专题", tags="etf,index"),
        _ds("fund_daily", "ETF日线行情", "ETF专题", 127, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount", domain="ETF专题", tags="etf,fund,行情"),
        _ds("fund_adj", "ETF复权因子", "ETF专题", 199, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,adj_factor", domain="ETF专题", tags="etf,fund,行情"),
        _ds("etf_share_size", "ETF份额规模", "ETF专题", 408, 6000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,close,nav,share,amount,market_value", domain="ETF专题", tags="etf,fund,规模"),
        _ds("fund_basic", "基金列表", "公募基金", 19, 2000, "ts_code,market,status", "ts_code,name,management,custodian,fund_type,found_date,due_date,list_date,issue_date,delist_date,status", domain="公募基金", tags="fund"),
        _ds("fund_company", "基金管理人", "公募基金", 118, 2000, "name,province", "name,shortname,province,city,address,phone,office,website,chairman,manager,reg_capital,setup_date", domain="公募基金", tags="fund"),
        _ds("fund_manager", "基金经理", "公募基金", 208, 2000, "ts_code,ann_date,name,offset,limit", "ts_code,ann_date,name,gender,birth_year,edu,nationality,begin_date,end_date,resume", domain="公募基金", tags="fund"),
        _ds("fund_share", "基金规模", "公募基金", 207, 2000, "ts_code,trade_date,start_date,end_date,fund_type,market", "ts_code,trade_date,fd_share", domain="公募基金", tags="fund,规模"),
        _ds("fund_nav", "基金净值", "公募基金", 119, 2000, "ts_code,end_date,start_date,market", "ts_code,ann_date,end_date,unit_nav,accum_nav,accum_div,net_asset,total_netasset,adj_nav", domain="公募基金", tags="fund,净值"),
        _ds("fund_div", "基金分红", "公募基金", 120, 2000, "ann_date,ex_date,pay_date,ts_code", "ts_code,ann_date,imp_anndate,base_date,div_proc,record_date,ex_date,pay_date,earpay_date,net_ex_date,div_cash", domain="公募基金", tags="fund,分红"),
        _ds("fund_portfolio", "基金持仓", "公募基金", 121, 2000, "ts_code,ann_date,end_date,symbol", "ts_code,ann_date,end_date,symbol,mkv,amount,stk_mkv_ratio,stk_float_ratio", domain="公募基金", tags="fund,持仓"),
        _ds("fund_factor_pro", "基金技术面因子(专业版)", "公募基金", 359, 5000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,open,high,low,close,pct_chg,vol,amount,turnover_rate,adj_factor,macd_bfq,rsi_bfq_6", domain="公募基金", tags="fund,因子"),
        _ds("index_basic", "指数基本信息", "指数专题", 94, 2000, "market,publisher,category,name", "ts_code,name,fullname,market,publisher,index_type,category,base_date,base_point,list_date,weight_rule,desc", domain="指数专题", tags="index"),
        _ds("index_daily", "指数日线行情", "指数专题", 95, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount", domain="指数专题", tags="index,行情"),
        _ds("index_weekly", "指数周线行情", "指数专题", 171, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount", domain="指数专题", tags="index,行情"),
        _ds("index_monthly", "指数月线行情", "指数专题", 172, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount", domain="指数专题", tags="index,行情"),
        _ds("index_dailybasic", "大盘指数每日指标", "指数专题", 128, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,total_mv,float_mv,total_share,float_share,free_share,turnover_rate,turnover_rate_f,pe,pe_ttm,pb", domain="指数专题", tags="index,估值"),
        _ds("index_weight", "指数成分和权重", "指数专题", 96, 2000, "index_code,trade_date,start_date,end_date,con_code", "index_code,con_code,trade_date,weight", domain="指数专题", tags="index,成分"),
        _ds("index_classify", "申万行业分类", "指数专题", 181, 2000, "index_code,level,src", "index_code,industry_name,level,industry_code,is_pub,parent_code,src", domain="指数专题", tags="index,行业"),
        _ds("index_member_all", "申万行业成分(分级)", "指数专题", 335, 2000, "l1_code,l2_code,l3_code,ts_code,is_new", "l1_code,l1_name,l2_code,l2_name,l3_code,l3_name,ts_code,name,in_date,out_date,is_new", domain="指数专题", tags="index,行业,成分"),
        _ds("sw_daily", "申万行业指数日行情", "指数专题", 327, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,name,open,low,high,close,change,pct_change,vol,amount", domain="指数专题", tags="index,行业,行情"),
        _ds("ci_daily", "中信行业指数日行情", "指数专题", 308, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,name,open,low,high,close,change,pct_change,vol,amount", domain="指数专题", tags="index,行业,行情"),
        _ds("ci_index_member", "中信行业成分", "指数专题", 373, 2000, "l1_code,l2_code,l3_code,ts_code,is_new", "l1_code,l1_name,l2_code,l2_name,l3_code,l3_name,ts_code,name,in_date,out_date,is_new", domain="指数专题", tags="index,行业,成分"),
        _ds("idx_factor_pro", "指数技术面因子(专业版)", "指数专题", 358, 5000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,open,high,low,close,pct_chg,vol,amount,turnover_rate,macd_bfq,rsi_bfq_6", domain="指数专题", tags="index,因子"),
        _ds("index_global", "国际主要指数", "指数专题", 211, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,swing,vol,amount", domain="指数专题", tags="index,global"),
        _ds("daily_info", "沪深市场每日交易统计", "指数专题", 215, 2000, "trade_date,ts_code,exchange,start_date,end_date", "trade_date,ts_code,ts_name,com_count,total_share,float_share,total_mv,float_mv,amount,vol,trans_count,pe,turnover_rate", domain="指数专题", tags="index,市场统计"),
        _ds("sz_daily_info", "深圳市场每日交易情况", "指数专题", 268, 2000, "trade_date,start_date,end_date", "trade_date,market,close,pct_change,turnover_rate,amount,pe,pe_ttm,pb,total_share,total_mv,float_share,float_mv", domain="指数专题", tags="index,市场统计"),
        _ds("cn_gdp", "国内生产总值(GDP)", "宏观经济", 227, 2000, "q,start_q,end_q", "quarter,gdp,gdp_yoy,pi,pi_yoy,si,si_yoy,ti,ti_yoy", domain="宏观经济", tags="macro,china"),
        _ds("cn_cpi", "居民消费价格指数(CPI)", "宏观经济", 228, 2000, "m,start_m,end_m", "month,nt_val,nt_yoy,nt_mom,nt_accu,town_val,town_yoy,town_mom,town_accu,cnt_val,cnt_yoy,cnt_mom,cnt_accu", domain="宏观经济", tags="macro,china"),
        _ds("cn_ppi", "工业生产者出厂价格指数(PPI)", "宏观经济", 245, 2000, "m,start_m,end_m", "month,ppi_yoy,ppi_mp_yoy,ppi_mp_qm_yoy,ppi_mp_rm_yoy,ppi_mp_p_yoy,ppi_cg_yoy,ppi_cg_f_yoy", domain="宏观经济", tags="macro,china"),
        _ds("cn_pmi", "采购经理指数(PMI)", "宏观经济", 325, 2000, "m,start_m,end_m", "month,pmi010000,pmi010100,pmi010200,pmi010300,pmi010400,pmi010500,pmi010600,pmi010700,pmi010800", domain="宏观经济", tags="macro,china"),
        _ds("cn_m", "货币供应量(月)", "宏观经济", 242, 2000, "m,start_m,end_m", "month,m0,m0_yoy,m0_mom,m1,m1_yoy,m1_mom,m2,m2_yoy,m2_mom", domain="宏观经济", tags="macro,china"),
        _ds("sf_month", "社融增量(月度)", "宏观经济", 310, 2000, "m,start_m,end_m", "month,inc_month,inc_cum,stk_end,stk_yoy", domain="宏观经济", tags="macro,china"),
        _ds("shibor", "Shibor利率", "宏观经济", 149, 2000, "date,start_date,end_date", "date,on,1w,2w,1m,3m,6m,9m,1y", domain="宏观经济", tags="macro,rate"),
        _ds("shibor_quote", "Shibor报价数据", "宏观经济", 150, 2000, "date,start_date,end_date,bank", "date,bank,on_b,on_a,1w_b,1w_a,2w_b,2w_a,1m_b,1m_a,3m_b,3m_a", domain="宏观经济", tags="macro,rate"),
        _ds("shibor_lpr", "LPR贷款基础利率", "宏观经济", 151, 2000, "date,start_date,end_date", "date,1y,5y", domain="宏观经济", tags="macro,rate"),
        _ds("libor", "Libor利率", "宏观经济", 152, 2000, "date,start_date,end_date,curr_type", "date,curr_type,on,1w,1m,2m,3m,6m,12m", domain="宏观经济", tags="macro,rate"),
        _ds("hibor", "Hibor利率", "宏观经济", 153, 2000, "date,start_date,end_date", "date,on,1w,2w,1m,2m,3m,6m,12m", domain="宏观经济", tags="macro,rate"),
        _ds("wz_index", "温州民间借贷利率", "宏观经济", 173, 2000, "date,start_date,end_date", "date,comp_rate,center_rate,micro_rate,cm_rate,sdb_rate,om_rate,aa_rate,m1_rate,m3_rate,m6_rate,m12_rate,long_rate", domain="宏观经济", tags="macro,rate"),
        _ds("gz_index", "广州民间借贷利率", "宏观经济", 174, 2000, "date,start_date,end_date", "date,d10_rate,m1_rate,m3_rate,m6_rate,m12_rate,long_rate", domain="宏观经济", tags="macro,rate"),
        _ds("us_tycr", "美国国债收益率曲线利率", "宏观经济", 219, 2000, "date,start_date,end_date", "date,m1,m2,m3,m6,y1,y2,y3,y5,y7,y10,y20,y30", domain="宏观经济", tags="macro,us,rate"),
        _ds("us_trycr", "美国国债实际收益率曲线利率", "宏观经济", 220, 2000, "date,start_date,end_date", "date,y5,y7,y10,y20,y30", domain="宏观经济", tags="macro,us,rate"),
        _ds("us_tbr", "美国短期国债利率", "宏观经济", 221, 2000, "date,start_date,end_date", "date,w4_bd,w4_ce,w8_bd,w8_ce,w13_bd,w13_ce,w26_bd,w26_ce,w52_bd,w52_ce", domain="宏观经济", tags="macro,us,rate"),
        _ds("us_tltr", "美国国债长期利率", "宏观经济", 222, 2000, "date,start_date,end_date", "date,ltc,treasury_20_year_rate,extrapolation_factor", domain="宏观经济", tags="macro,us,rate"),
        _ds("us_trltr", "美国国债长期实际利率平均值", "宏观经济", 223, 2000, "date,start_date,end_date", "date,y10,y20,y30", domain="宏观经济", tags="macro,us,rate"),
        _ds("news", "新闻快讯(短讯)", "大模型语料专题数据", 143, 6000, "src,start_date,end_date", "datetime,content,title,channels", domain="大模型语料专题数据", tags="news,llm"),
        _ds("major_news", "新闻通讯(长篇)", "大模型语料专题数据", 195, 6000, "src,start_date,end_date", "datetime,title,content,src,category", domain="大模型语料专题数据", tags="news,llm"),
        _ds("cctv_news", "新闻联播文字稿", "大模型语料专题数据", 154, 6000, "date,start_date,end_date", "date,title,content", domain="大模型语料专题数据", tags="news,llm,policy"),
        _ds("anns_d", "上市公司公告", "大模型语料专题数据", 176, 6000, "ts_code,ann_date,start_date,end_date", "ts_code,ann_date,name,title,url,rec_time", domain="大模型语料专题数据", tags="announcement,llm"),
        _ds("npr", "国家政策库", "大模型语料专题数据", 406, 6000, "start_date,end_date,source", "pub_time,title,content,source,url", domain="大模型语料专题数据", tags="policy,llm"),
        _ds("research_report", "券商研究报告", "大模型语料专题数据", 415, 6000, "ts_code,start_date,end_date,keyword", "ts_code,name,title,org,author,pub_date,summary,url", domain="大模型语料专题数据", tags="research,llm"),
        _ds("irm_qa_sh", "上证e互动问答", "大模型语料专题数据", 366, 6000, "ts_code,start_date,end_date", "ts_code,name,trade_date,question,answer", domain="大模型语料专题数据", tags="irm,llm"),
        _ds("irm_qa_sz", "深证易互动问答", "大模型语料专题数据", 367, 6000, "ts_code,start_date,end_date", "ts_code,name,trade_date,question,answer", domain="大模型语料专题数据", tags="irm,llm"),
        _ds("hk_basic", "港股基础信息", "港股数据", 191, 2000, "ts_code,list_status", "ts_code,name,fullname,enname,cn_spell,market,list_status,list_date,delist_date,trade_unit,isin", domain="港股数据", tags="hk"),
        _ds("hk_tradecal", "港股交易日历", "港股数据", 250, 2000, "start_date,end_date,is_open", "cal_date,is_open,pretrade_date", domain="港股数据", tags="hk,calendar"),
        _ds("hk_daily", "港股日线行情", "港股数据", 192, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount", domain="港股数据", tags="hk,行情"),
        _ds("hk_daily_adj", "港股复权行情", "港股数据", 339, 6000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount,adj_factor", domain="港股数据", tags="hk,行情"),
        _ds("hk_adjfactor", "港股复权因子", "港股数据", 401, 6000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,adj_factor", domain="港股数据", tags="hk,行情"),
        _ds("us_basic", "美股基础信息", "美股数据", 252, 2000, "ts_code,list_status", "ts_code,name,enname,market,list_status,list_date,delist_date,exchange,isin", domain="美股数据", tags="us"),
        _ds("us_tradecal", "美股交易日历", "美股数据", 253, 2000, "start_date,end_date,is_open", "cal_date,is_open,pretrade_date", domain="美股数据", tags="us,calendar"),
        _ds("us_daily", "美股日线行情", "美股数据", 254, 2000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount,vwap,turnover_ratio,total_mv,pe,pb", domain="美股数据", tags="us,行情"),
        _ds("us_daily_adj", "美股复权行情", "美股数据", 338, 6000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount,adj_factor", domain="美股数据", tags="us,行情"),
        _ds("us_adjfactor", "美股复权因子", "美股数据", 402, 6000, "ts_code,trade_date,start_date,end_date", "ts_code,trade_date,adj_factor", domain="美股数据", tags="us,行情"),
    ]
}

TUSHARE_DATASETS: dict[str, TushareStockDataset] = {
    **TUSHARE_STOCK_DATASETS,
    **TUSHARE_COMMON_DATASETS,
}


def get_tushare_dataset(dataset: str) -> TushareStockDataset:
    key = str(dataset or "").strip().lower()
    if key not in TUSHARE_DATASETS:
        raise ValueError(f"unsupported Tushare dataset: {dataset}")
    return TUSHARE_DATASETS[key]


def get_tushare_stock_dataset(dataset: str) -> TushareStockDataset:
    key = str(dataset or "").strip().lower()
    if key not in TUSHARE_STOCK_DATASETS:
        raise ValueError(f"unsupported Tushare stock dataset: {dataset}")
    return TUSHARE_STOCK_DATASETS[key]


def list_tushare_datasets(
    *,
    domain: str | None = None,
    category: str | None = None,
    include_deprecated: bool = True,
    tags: list[str] | tuple[str, ...] | str | None = None,
) -> list[dict[str, Any]]:
    selected = sorted(TUSHARE_DATASETS.values(), key=lambda item: (item.domain, item.category, item.dataset))
    if domain:
        selected = [item for item in selected if item.domain == domain]
    if category:
        selected = [item for item in selected if item.category == category]
    if not include_deprecated:
        selected = [item for item in selected if item.status != "deprecated"]
    tag_filter = _tag_filter(tags)
    if tag_filter:
        selected = [item for item in selected if tag_filter & set(item.tags)]
    return [item.to_dict() for item in selected]


def list_tushare_stock_datasets(
    *,
    category: str | None = None,
    include_deprecated: bool = True,
) -> list[dict[str, Any]]:
    selected = sorted(TUSHARE_STOCK_DATASETS.values(), key=lambda item: (item.category, item.dataset))
    if category:
        selected = [item for item in selected if item.category == category]
    if not include_deprecated:
        selected = [item for item in selected if item.status != "deprecated"]
    return [item.to_dict() for item in selected]


def _tag_filter(tags: list[str] | tuple[str, ...] | str | None) -> set[str]:
    if tags is None:
        return set()
    if isinstance(tags, str):
        raw = _fields(tags)
    else:
        raw = tuple(str(item).strip() for item in tags if str(item).strip())
    return set(raw)
