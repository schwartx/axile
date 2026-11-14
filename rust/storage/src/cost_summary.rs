//! 与浏览器共用成交成本聚合口径。
pub(crate) use axile_analysis_core::cost::Summary;
impl axile_analysis_core::cost::CostSource for crate::fact_trades::Trade {
    fn cost_input(&self) -> axile_analysis_core::cost::CostInput<'_> {
        axile_analysis_core::cost::CostInput {
            value: self.value,
            cost: self.cost,
            loss_bp: self.loss_bp,
            fee: self.fee,
            fee_currency: self.fee_currency.as_deref(),
            estimated: self.estimated,
        }
    }
}
