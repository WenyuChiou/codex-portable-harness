# 換電腦安裝與驗收

這是本次 Windows 安裝經驗整理出的可重跑流程。核心 harness 仍可使用
Python 3.10+；目前研究工具依賴清單驗證於 Python 3.12 / Windows。
其他平台的來源部署與離線測試不等於該平台的完整實機驗收。

## 首次安裝

先準備 Git、Python 3.12（含 venv/pip）、Node.js/npm、Google Chrome，以及
已登入且能執行原生 sandbox 的 Codex CLI。私有來源需要既有的 GitHub Git
存取權。安裝器不會索取或把 GitHub token 寫進 repo。

在 PowerShell 中：

```powershell
$codexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
git clone https://github.com/WenyuChiou/codex-portable-harness.git (Join-Path $codexRoot 'portable-harness')
Set-Location (Join-Path $codexRoot 'portable-harness')
.\setup.ps1
.\setup.ps1 -Apply -RepairPythonAcl -LiveAgents
```

若 `python` 不是 3.12，可指定實際路徑；harness 與研究環境可用不同版本：

```powershell
$researchPython = & py -3.12 -c 'import sys; print(sys.executable)'
.\setup.ps1 -Apply -ResearchPython $researchPython -RepairPythonAcl -LiveAgents
```

預設只顯示計畫，不下載、不建立環境、不登入、不改 PATH/ACL。
`-Apply` 會建立或沿用兩個隔離環境，安裝 repo 與 SHA-256 固定的 public
harness wheel，再部署五個 baseline agents、兩個核心 skills、17 個研究
skills、四個 Codex TOML agents，以及 Research Hub、Playwright、文書/PDF/
Zotero helpers、Defuddle 與 Node `docx`。缺少 npm/Git/CLI 或 Python 版本不符會明確失敗。

Antigravity 若已有安裝就沿用，保留原登入；否則 Windows 從官方 updater
manifest 下載，驗證 SHA-512 後放入 `CODEX_HOME/tools/agy`。該工具採官方
更新通道，版本會隨時間改變；`antigravity_tested_version` 只記錄已測版本，
不是保證未來下載相同版本。Node/Python 套件版本固定於 profile/constraints，
但沒有聲稱所有平台的 wheel 位元完全相同。

## 安裝內容與重跑

| 位置（相對於 CODEX_HOME） | 用途 |
| --- | --- |
| portable-harness-runtime | harness 專用 Python 與固定 public dependency |
| research-runtime | Python 3.12 研究環境，不能以 system Python 代替驗證 |
| skills、agents | 原生可載入定義；額外 agents 繼承 parent model/effort |
| custom-agent-sources/dotfiles-claude | 指定 commit 的原始 Markdown，沒有套用其他產品設定 |
| machine-setup/research-ownership.json | 已部署檔案雜湊與恢復紀錄 |
| machine-setup/installation.json | 本機工具路徑與未完成步驟 |
| machine-setup/activate.ps1、activate.sh | 本機 PATH 與研究環境設定；不修改全域模型或 registry |
| machine-setup/research-hub-config.json、research-vault | 新機專用設定與空白研究根目錄 |
| machine-setup/compatibility | NotebookLM 原始檔與補丁雜湊 |
| machine-setup/acl-backups | Windows 權限修復前的原始 ACL |
| machine-setup/agent-smoke | 每次原生派遣的隔離 fixture、結果與驗收證據 |

`profiles/research.json` 固定各來源 repo 與 commit。部署先檢查所有目標，
遇到未知同名檔或使用者修改就拒絕覆蓋；不刪除自訂 agents，不執行
`research-hub install`。不要用「強制重裝」抹掉衝突。每個已發布檔案都記錄
ownership，部分中斷後先查差異再重跑；這不是跨全部套件的原子交易。
安裝入口在使用既有 Python 或寫入 runtime/cache/vault 前會拒絕 managed
目錄中的 junction／symlink，避免沿連結寫到 CODEX_HOME 外。

`CODEX_HOME` 可自訂為絕對路徑，未設定則為 `~/.codex`；其他 agent 產品的
home 不可作為目標。候選模型升級仍須本機 benchmark，不會從舊機搬 admission。
新開 Codex session 才會載入新 skills/agents。

目前研究設定與空白 vault 都放在 CODEX_HOME 內，預設不啟用 Zotero。
既有外部 vault、platformdirs 設定與 NotebookLM 登入不會自動移轉或覆蓋。
完成後在要使用工具的 PowerShell 載入：

```powershell
. (Join-Path $codexRoot 'machine-setup/activate.ps1')
```

此處提供的 `research-hub` launcher 明確指定本機 config 和 NotebookLM profile。
Activation 同時設定 `NODE_PATH`，讓 skills 的 Word 匯出腳本找到本機 `docx`。
要沿用其他 vault，請在本機設定，並另外驗證；不要把個人設定提交到 repo。

## 分層驗收

```powershell
$harnessPython = Join-Path $codexRoot 'portable-harness-runtime/Scripts/python.exe'
& $harnessPython scripts/doctor.py --cross-tool --diff
& $harnessPython scripts/verify_installation.py --sandbox --save
& $harnessPython scripts/functional_checks.py
& $harnessPython scripts/agent_smoke.py --run
```

第一層檢查 canonical baseline/政策。第二層比對研究部署雜湊、九個指定 agents、
兩個隔離環境的身分、標準 extension/研究套件匯入、同一 launcher 的孫程序、
`pip check` 和 CLI 啟動；Windows 另外在 `elevated` / `:workspace` sandbox
執行原本的 venv launcher，沒有 system Python fallback。

離線功能檢查會在 CODEX_HOME 產生獨立 fixture：實際執行兩個 Word exporter，
解開產物確認文字、執行 Defuddle 本機 HTML extraction、Research Hub workflow
init/validate，以及 Bash wrappers 語法與 GNU timeout 檢查。wrapper 的付費
派遣本身不在語法檢查範圍；不會偷偷執行 upstream wrapper 的寬權限模式。

第三層會產生付費模型呼叫：三批、每批至多三個 children、同時一個，對九個
指定 agent type 實際 spawn、wait、收集原生 final answer。每次 spawn 前用
public policy adapter 評估 checkpoint；保留 metrics、失敗與父 session 的
精確證據，只讀取本次產生的 trace。驗收檢查原生身分、無 child 模型 override、
final answer 一致、fixture/agent 定義保存、CSV 輸出及 worker 四個測試。
控制器不使用 `--ephemeral`：先前實測 ephemeral session 無法派遣 children。
控制器需要的 model 可用 `--controller-model` 指定；不替換 agent 定義的模型。
修正單一角色後可用 `--agent harness-drift-monitor` 做指定角色驗收，報告會列出
實際選取的 agents，不能把這類結果寫成九個 agents 全部重跑。
缺模型、逾時、policy stop、拒絕或失敗皆保留為失敗，不自動重試或放寬權限。

這些是有限任務的安裝驗收，不是任意工作成功率、效能、價格或品質 benchmark。
離線 CI 不會登入帳號或呼叫模型；不能把 CI 綠燈當作已完成新機的原生派遣。

較深的 Windows 安裝路徑曾讓 pip 的 Git checkout 因 `Filename too long`
失敗。安裝器會保留既有 Git config environment entries，僅在該程序及其
子程序附加 `core.longpaths=true`，不改全域 Git config 或 registry。
Python build backend 在未啟用 OS 長路徑的 Windows 上仍可能無法讀取 Git
已建立的長檔名。因此暫存目錄直接放在 CODEX_HOME/tmp，並在建立研究環境前
依固定來源的最長 package 路徑檢查長度；過深的 CODEX_HOME 會明確要求縮短，
不繼續做出一半的環境。不是宣稱任意深度的 Windows 路徑都能運作。
完整 deployment hash audit 使用 `audit_deployment.py`，避免 PowerShell
constrained-language mode 不支援部分路徑正規化方法。

## Windows Python 啟動失敗

本次故障的實際原因：venv 存在，但它指向使用者 AppData 下的 base Python，
該安裝目錄的 ACL 不允許 `CodexSandboxUsers` 執行。venv redirector 的訊息
容易被誤讀為 Python 不存在；直接在相同 sandbox 啟動 base interpreter
會得到 `CreateProcessAsUserW failed: 5`（access denied）。

```powershell
.\scripts/repair_windows_python.ps1           # 診斷/計畫
.\scripts/repair_windows_python.ps1 -Apply    # 明確授權此項窄範圍 ACL 修復
```

腳本先確認 venv 在 sandbox 外正常、base 目錄是實際 Python 安裝，且沒有
reparse-point 路徑；只對已重現 error 5 的情況做修復。完整備份 ACL 後，僅對
base Python 子樹加上現有 CodexSandboxUsers 群組的可繼承 ReadAndExecute，
不授予寫入、不修改 Everyone、不改 sandbox/network/approval。
原 launcher 已能執行則不變更 ACL。權限繼承可能耗時，應等待結束再驗證。
其他啟動錯誤會停止診斷，不把它們誤當 ACL 問題。
安裝入口的 `-RepairPythonAcl` 會分別檢查 harness 與研究環境的原 launcher；
兩者使用不同 base Python 時，也各自依診斷結果處理。

備份的 repair.json 保存 target/parent。需要還原時，由操作者使用
`icacls.exe <recorded-parent> /restore <backup-file>`；還原也會恢復原本的限制。
Python 重裝或搬動後必須重新檢查。

## 仍需本機登入的整合

安裝成功與已登入分開記錄。工具不讀取/輸出 cookie、API key 或 OAuth code，
也不複製舊機憑證。依序在本機進行：

1. 執行 `agy`，完成當次 Google 登入。過期授權碼不能沿用。用
   `agy --sandbox --mode plan --print 'Reply exactly AGY_READY. No tools.' --output-format json`
   驗證當次 authenticated canary；必須看到 SUCCESS 與指定回覆。
2. 載入 activate.ps1 後執行
   `research-hub notebooklm login --auto-detect --wait-timeout 300`，在瀏覽器完成登入。
   離線補丁支援 Windows 系統 Chrome、多分頁與 Google-owned
   `notebook.google.com` redirect，保留精確 HTTPS hostname 和 signed-in cookie gate。
   不要只憑「看見首頁」就宣布成功；必須由工具保存後執行 NotebookLM 的唯讀
   health/server probe，確認 server 接受此次 session：

   ```powershell
   $researchPython = Join-Path $codexRoot 'research-runtime/Scripts/python.exe'
   $stateFile = Join-Path $codexRoot 'research-vault/.research_hub/nlm_sessions/state.json'
   & $researchPython scripts/check_services.py --network --notebooklm-state $stateFile
   ```
3. Zotero 是獨立可選步驟，這次使用者選擇略過。需要完整讀寫時，在本機
   安全設定 key/library ID，再用 helper 的 `credentials_status()` 與唯讀連線確認；不要
   將 key 貼到聊天、命令參數、repo 或驗收報告。

Antigravity 的可重跑 canary（模型呼叫）也可以使用：

```powershell
& $researchPython scripts/check_services.py --network --antigravity (Get-Command agy).Source
```

未明確傳入 `--network` 與指定服務時，不呼叫 provider，回傳 NOT_CHECKED。
驗證只輸出狀態，不輸出 notebook 內容、授權資料或 provider 錯誤原文。

NotebookLM 補丁只接受指定 upstream SHA-256，保留原始檔並記錄 patched hash；
重跑已知補丁為 no-op，未知修改/新版 upstream 一律停止，不做模糊字串套用。
Chrome 缺失時需安裝 Chrome 或自行驗證 Playwright browser runtime；套件成功
匯入並不代表瀏覽器已能啟動。Research Hub 升級後必須重驗補丁與登入。

參考：[OpenAI Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)、
[CLI sandbox options](https://learn.chatgpt.com/docs/cli/reference)、
[native subagent contract](https://learn.chatgpt.com/docs/agent-configuration/subagents)、
[Antigravity official installer](https://antigravity.google/cli/install.ps1)。
