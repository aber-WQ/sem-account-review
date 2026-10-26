# 动作检查器契约

`scripts/check_actions.py`接受一个UTF-8 JSON，检查标准台账结构。它不自动生成否词，不抓网站，不实现任何广告平台的匹配引擎，也不直接修改账户。模型必须先依据原始资料完成实际作用域核验，再导出本契约。

## 顶层结构

```json
{
  "schema_version": "1.0",
  "relations": [],
  "actions": [],
  "priority_action_ids": []
}
```

`relations`为全量已分析搜索关系，`actions`为唯一原子动作台账，`priority_action_ids`是09的顺序。无搜索关系时允许空列表，不能为填表造关系。若需多事件/多币种事实，与事实文件通过关系ID和事件/周期键关联；本检查器不复算费用。

## 关系记录

每条包括：

- `relationship_id`：唯一内部关系ID，与事实文件一致；使用`sem_data.py`时直接取其`relation_id`，只是字段名转换，不重新生成身份。同一关系的不同周期/事件事实共享这个ID。
- `diagnosis`：D1/D2/D3/D4/D5。
- `retain_in_scope`：布尔值；当前同作用域是否仍需要保留该关系。路由排除必须先明确原作用域不保留、目标作用域有前置承接动作。
- `current_negative_state`：`none / pending / executable`（不否定/待核验否定/可执行否定）。
- `current_action_ids`：关联动作ID列表。包括非来源但被最终动作覆盖的关系。

关键词目标、搜索意图、证据、置信度、来源等语义字段保留在分析明细；本契约可附加这些字段但不以其存在代替语义复核。

## 动作记录

共同必填字段：

- `action_id`：唯一ID。
- `action_type`：`scope_change / keyword_change / negative / new_keyword`。
- `record_type`：`change / verification / observation`，分别为变更/核验任务/观察。
- `status`：`executable / verify / observe / rejected`。
- `priority`：P1/P2/P3；`origin_sheet`：05/06/07/08，与动作类型依次对应。
- `target`：至少包含`account_id, level, scope_id`。必须能对应真实对象或明确的内部来源作用域；内部ID不得冒充平台ID。
- `source_relationship_ids, covered_relationship_ids, dependencies`：列表，可为空；设置动作可由对象级证据支持，不能为凑来源而虚构关系。
- `evidence_refs`：证据引用列表。可执行变更必须有能实际访问的依据；检查器只检查非空，不证明依据真实。
- `checks`：已实际核验的布尔标记；缺失和`false`都不能通过必要检查。

可执行变更的共同检查：`object_verified, scope_verified, conflicts_resolved`。否词另需`platform_semantics_verified, assets_checked`；新增另需`capability_supported, business_allowed, campaign_scope_allowed, assets_checked`；其他设置变更另需`current_setting_verified, operation_supported`。业务允许、能力支持、本次目标投放范围允许分别核验，不能用其中一项替代另一项。

可执行否词的`target`另包含`text, match_type`；可执行新增另包含`text, match_type`；可执行设置变更另包含`field, value`，并提供`current_value`。这些值的语义可操作性由来源核验，不由检查器猜测。

核验任务本身可为`executable`，表示可以开始核验，不表示其关联账户变更已可执行。只有`record_type=change && status=executable`会产生可执行否定回写。依赖必须引用同一台账，不能成环；一个可执行变更的前置任务必须同样可执行，执行顺序仍要遵守依赖。

## 检查结果的解释

检查器覆盖：唯一ID、枚举、来源/覆盖引用、动作主表、必要条件标记、待核验与可执行状态、否词对保留关系的冲突、全部受影响关系回写、明显不合格来源的新增、依赖环与09顺序。返回非零退出码表示结构或一致性错误。

它不核验：网页事实真伪、受众推断、平台当前否词语义、是否遗漏覆盖查询、真实资产是否完整、当前设置是否支持变更、财务指标或语义准确率。完整验收仍依[质量检查](quality-checks.md)。
