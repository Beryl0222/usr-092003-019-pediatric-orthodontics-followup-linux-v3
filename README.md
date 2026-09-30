# 儿童活动矫治器制作与交接履历后端

为口腔机构建立每件儿童活动矫治器从数字印模到跨院交接的完整履历：
数字印模校验值、医生处方版本、加工参数、材料批次、技师复核、试戴结论、
交付说明、监护确认与后续异常全部串联留痕；处方变更不覆盖已经发生的制作事实；
系统只提示风险与受影响范围，不代替医生作临床判断。

## 核心边界

- **重复扫描不产生两件器械**：扫描以 `算法:十六进制校验值` 唯一索引；同人重复上传返回原记录；
  同一文件挂到另一位患儿名下生成人工裁决单（合并重复档案 / 驳回误挂），裁决前禁止制作。
- **姓名转写差异交人工**：同一证件不同姓名（同音字、简写）不自动新建档案，档案置为待确认，
  医生确认法定姓名前不得开制。
- **处方变更的批准责任**（版本只追加，历史不覆盖）：
  - 排产前：责任医生直接启用新版；
  - 已排产/制作中：责任医生明确**终止**或**返工**；返工须承担该件的加工中心负责人会签；
  - 已成成品/已交付：只能**返工**（加工方会签）或由医生批准现有成品**继续使用**，
    不能终止既成事实。
- **制作事实不可变**：终止/返工的旧批次保留状态、材料批次快照、制作人与复核人；
  快照在排产时固化处方版本、印模校验值与加工参数。
- **内控**：未复核校验值的印模不得开制；制作者不能复核本人的件；QC 不合格不得试戴；
  返工后的新批次必须重新试戴，旧试戴结论不能沿用。
- **异常有期限、有责任人**：丢失 10 日、破损 7 日、过敏疑点 7 日、批次召回 15 日（可覆盖）。
  - 丢失补制：须先结案异常，且医生显式签署印模仍有效（`reuse_scan_attested`），
    任何手机照片都不足以"照旧重做"；新件与原件通过 `replacement_of/replaced_by` 成链。
  - 过敏疑点：仅列出该件用过的材料批号并提示就医判断，不作因果断言。
  - 批次召回：加工方发起后逐件挂召回处置单，圈定受影响范围；召回批次禁止再用于制作。
- **授权与最小化**：
  - 加工方须先被指派才能接单与查看；履历视图仅含制作所需的印模文件与校验值、
    处方制作参数、自家批次记录，**无姓名、无证件、无影像**；
  - 监护人经授权后查看试戴结论、交付说明、物流与处置进度；
  - 归属门诊或监护人均可授权异地接诊门诊（只读 / 查看并随访），授权留痕。
- **物流不泄身份**：普通物流单禁止携带影像、印模文件、身份证件；面单不含患儿姓名，
  运单号脱敏（如 `SF****90`）。
- **跨院交接**：存在未结异常时，必须逐项带齐责任人与截止时间；接收门诊逐项确认后才完成归属转移。
- **全程可反查**：任何一件器械都能反查所用扫描、处方版本、材料批次、制作与复核人员、
  试戴结论、交付说明、监护确认与交接记录，另有只追加的审计轨迹。

## 运行

```bash
pip install -r requirements.txt
python3 service.py --check                    # 配置自检
python3 service.py --port 8000                # 启动服务
curl http://127.0.0.1:8000/health
python3 -m unittest discover -s tests -v      # 30 项领域与 HTTP 契约测试
```

## 结构

- `ledger.py`：与框架无关的履历领域核心（线程安全，内存存储）。
- `service.py`：标准库 HTTP 适配层，Bearer 令牌（令牌即用户 ID，联调用）、
  机构/用户引导接口使用 `X-Bootstrap-Token`（默认 `bootstrap-dev-token`）。
- `tests/test_ledger.py`：领域规则测试，含开篇"异地丢件、照片不能照旧重做"全链路场景。
- `tests/test_http.py`：鉴权、错误码与跨角色 API 链路。

## 主要接口

| 方法 & 路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /orgs`、`POST /users` | 引导令牌 | 登记机构与用户 |
| `POST /patients` | 门诊 | 登记患儿（同名异写挂起人工确认） |
| `POST /patients/{id}/confirm-identity` | 医生 | 人工确认法定姓名 |
| `POST /patients/{id}/guardians` | 门诊 | 绑定授权监护人 |
| `POST /patients/{id}/clinic-authorizations` | 门诊/监护人 | 授权异地门诊（VIEW_ONLY / VIEW_AND_FOLLOWUP） |
| `POST /scans` | 门诊 | 上传扫描（重复自动去重，跨档案冲突挂起） |
| `POST /scan-conflicts/{id}/review` | 门诊 | 人工裁决：MERGE_DUPLICATE_RECORD / REJECT_UPLOAD |
| `POST /scans/{id}/verify` | 医生 | 印模校验值复核签名 |
| `POST /prescriptions` | 医生 | 创建处方（v1） |
| `POST /prescriptions/{id}/revisions` | 医生 | 提交新版（PENDING_APPROVAL） |
| `POST /prescriptions/{id}/approvals` | 医生（+加工方会签） | ACTIVATE / TERMINATE / REWORK / CONTINUE |
| `POST /devices` | 医生 | 建档开制（补制须 replacement_of + reuse_scan_attested） |
| `POST /devices/{id}/assign-lab` | 门诊 | 指派加工中心 |
| `POST /devices/{id}/schedule` | 加工中心 | 排产并快照版本/印模/参数 |
| `POST /runs/{id}/start` | 技师 | 开始制作（绑定材料批次，召回批次禁用） |
| `POST /runs/{id}/check` | 另一技师 | 复核 PASS/FAIL |
| `POST /runs/{id}/remake` | 加工中心 | QC 不合格内部返工（处方版本不变） |
| `POST /devices/{id}/fittings` | 医生 | 试戴结论 OK/ADJUSTED/FAIL |
| `POST /devices/{id}/deliveries` | 门诊 | 交付说明（当前批次试戴通过为前提） |
| `POST /devices/{id}/guardian-confirmations` | 监护人 | 逐项勾选确认并签名 |
| `POST /devices/{id}/shipments` | 门诊 | 物流登记（拒绝影像/身份附件，返回脱敏面单） |
| `POST /devices/{id}/exceptions` | 门诊/监护人 | LOSS / DAMAGE / ALLERGY_SUSPECTED |
| `POST /exceptions/{id}/resolve`、`/assign` | 门诊 | 结案与责任人指派 |
| `POST /materials/batches` | 加工中心 | 材料批次登记 |
| `POST /materials/batches/{id}/recall` | 加工中心 | 批次召回，逐件挂处置单 |
| `POST /devices/{id}/transfers` | 归属门诊 | 发起跨院交接（未结异常须带责任人和期限） |
| `POST /transfers/{id}/accept` | 接收门诊 | 逐项确认异常并接收 |
| `GET  /devices/{id}/dossier` | 授权方 | 三种投影：CLINIC_FULL / LAB_MINIMIZED / GUARDIAN |
| `GET  /devices/{id}/risk` | 授权方 | 风险提示与召回影响范围（仅提示） |
| `GET  /exceptions?status=OPEN&overdue=1` | 授权方 | 异常与逾期清单 |
| `GET  /audit?device={id}` | 机构 | 只追加的审计轨迹 |

错误以 `{"error": ..., "type": ...}` 返回，HTTP 状态码：400 校验失败、401 未认证、
403 越权、404 不存在、409 冲突（如重复扫描跨档案）、422 状态不允许。

## 明确不做的事

- 不根据照片、旧版本直接复制器械；
- 不自动判定过敏因果、不替医生决定停用/更换；
- 不向加工方、物流渠道暴露患儿身份材料与影像；
- 不用新数据覆盖历史批次、历史处方与已交付事实。
