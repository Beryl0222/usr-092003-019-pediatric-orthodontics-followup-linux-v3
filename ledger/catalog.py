"""角色、制作阶段、异常类型与处置期限。

系统只提示风险与受影响范围，不替医生作临床判断；因此所有期限只用于
驱动处置时限，任何医学结论仍由具备资质的角色记录。
"""

# 角色
ROLE_PRESCRIBING_DOCTOR = "prescribing_doctor"        # 处方医生
ROLE_RECEIVING_DOCTOR = "receiving_doctor"            # 接诊医生
ROLE_TECHNICIAN = "technician"                        # 加工技师（含复核）
ROLE_FABRICATION_ADMIN = "fabrication_admin"          # 加工方排产/管理
ROLE_GUARDIAN = "guardian"                            # 监护人
ROLE_CLINIC_ADMIN = "clinic_admin"                    # 门诊管理人员

# 器械生命周期阶段（只能向前推进，返工另开版本事件，不回滚事实）
STAGE_SCAN_RECEIVED = "scan_received"
STAGE_PRESCRIBED = "prescribed"
STAGE_SCHEDULED = "scheduled"          # 已排产
STAGE_IN_PRODUCTION = "in_production"
STAGE_TECH_REVIEWED = "tech_reviewed"  # 技师复核通过
STAGE_FINISHED = "finished"            # 成品
STAGE_FITTED = "fitted"                # 试戴完成
STAGE_DELIVERED = "delivered"          # 交付且监护确认
STAGE_SUPERSEDED = "superseded"        # 已被新版本取代
STAGE_TERMINATED = "terminated"        # 终止，不再继续制作

STAGE_ORDER = [
    STAGE_SCAN_RECEIVED,
    STAGE_PRESCRIBED,
    STAGE_SCHEDULED,
    STAGE_IN_PRODUCTION,
    STAGE_TECH_REVIEWED,
    STAGE_FINISHED,
    STAGE_FITTED,
    STAGE_DELIVERED,
]

# 处方变更裁决
DISPOSITION_TERMINATE = "terminate"              # 终止：旧件不再使用
DISPOSITION_REWORK = "rework"                    # 返工：基于新处方重新制作
DISPOSITION_CONTINUE = "continue_use"            # 继续使用：旧件照常流转
# 重制不作为独立裁决：旧件先依批准终止，再由医生显式按原扫描建新件（remade_from 留谱系）

DISPOSITIONS = {
    DISPOSITION_TERMINATE,
    DISPOSITION_REWORK,
    DISPOSITION_CONTINUE,
}

# 成品之后只允许终止或继续使用（继续使用需书面理由）；排产之后可返工
DISPOSITIONS_BEFORE_SCHEDULE = DISPOSITIONS
DISPOSITIONS_AFTER_SCHEDULE = DISPOSITIONS
DISPOSITIONS_AFTER_FINISH = {DISPOSITION_TERMINATE, DISPOSITION_CONTINUE}

# 异常类型
ISSUE_LOSS = "loss"                # 丢失
ISSUE_DAMAGE = "damage"            # 破损
ISSUE_ALLERGY_SUSPECTED = "allergy_suspected"   # 过敏疑点
ISSUE_BATCH_RECALL = "batch_recall"              # 批次召回

# 各异常处置期限（自然日），从登记时刻起算
SLA_DAYS = {
    ISSUE_LOSS: 7,
    ISSUE_DAMAGE: 5,
    ISSUE_ALLERGY_SUSPECTED: 3,
    ISSUE_BATCH_RECALL: 10,
}

# 异常状态
ISSUE_OPEN = "open"
ISSUE_ACTION_TAKEN = "action_taken"   # 已采取处置（如停用、召回通知）
ISSUE_RESOLVED = "resolved"
ISSUE_OVERDUE = "overdue"             # 派生状态，不单独落事件

# 过敏疑点在医生结论前，系统只做风险提示并建议暂停使用，不自动判定过敏
RISK_NOTICE = {
    ISSUE_LOSS: "器械丢失：需评估重制并核对身份，旧件应标记失效。",
    ISSUE_DAMAGE: "器械破损：继续佩戴可能造成损伤，建议停用待医生评估。",
    ISSUE_ALLERGY_SUSPECTED: (
        "过敏疑点：系统不判定过敏。建议暂停佩戴并保留材料批次信息，由医生结合临床表现判断。"
    ),
    ISSUE_BATCH_RECALL: "批次召回：以下同批次器械需全部排查，处置结论由各接诊医生分别作出。",
}

# 交接状态
HANDOVER_PENDING = "pending_acceptance"
HANDOVER_ACCEPTED = "accepted"
HANDOVER_REJECTED = "rejected"
