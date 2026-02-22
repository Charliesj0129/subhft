# An end-to-end data-driven optimisation framework for constrained trajectories

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： An end-to-end data-driven optimisation framework for constrained trajectories
• **作者**： Florent Dewez, Benjamin Guedj, Arthur Talpaert, Vincent Vandewalle (Inria & UCL)
• **年份**： 2020 (ArXiv 2021 v2)
• **期刊/會議**： ArXiv:2011.11820
• **引用格式**： Dewez, F., Guedj, B., Talpaert, A., & Vandewalle, V. (2020). An end-to-end data-driven optimisation framework for constrained trajectories. arXiv preprint arXiv:2011.11820.
• **關鍵詞**： #Trajectory_Optimization #Data-Driven #Constrained_Optimization #PyRotor #Aeronautics
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[Optimal_Execution]], [[Control_Theory]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Gap (研究缺口)**：

- 傳統的軌跡優化（Trajectory Optimization）通常基於最優控制（Optimal Control），需要精確的系統動力學模型（ODE/PDE）。
- 在許多現實場景中（如飛行器、帆船），系統動力學可能未知（Black-box）或過於複雜（計算昂貴）。
- 僅依賴數據重建動力學（Reconstruction）會引入誤差，且不直接解決優化問題。

• **研究目的**：

- 提出一個 **End-to-End Data-Driven** 框架，無需顯式的動力學模型。
- 直接利用觀察到的軌跡數據（Reference Trajectories），通過統計建模與貝葉斯方法，尋找既優化成本又符合物理約束的軌跡。
- 開發開源 Python 庫 **PyRotor**。

• **理論框架**：

- **Functional Data Analysis**: 將無限維軌跡投影到有限維基函數空間（Basis Functions）。
- **Bayesian Framework (MAP)**: 利用 Maximum A Posteriori 估計，將參考軌跡作為先驗知識（Prior）。

---

### 🛠 研究方法論 (Methodology - 深度拆解)

• **研究範式**： 統計學習 + 優化 (Statistical Learning + Optimization)

• **核心模型**：

1. **軌跡投影 (Projection)**：
   - 將軌跡 $y(t)$ 投影到正交基（如 Legendre polynomials）上，得到係數向量 $c \in \mathbb{R}^K$。
   - $y(t) \approx \sum_{k=1}^{K} c_k \phi_k(t)$。
2. **參考軌跡建模**：
   - 假設觀測到的參考軌跡 $\{y^R_i\}$ 是最優軌跡 $y^*$ 的噪聲觀測：$c^R_i = c^* + \epsilon_i$。
   - 噪聲 $\epsilon_i$ 服從協方差矩陣 $\Sigma$ 的高斯分佈。$\Sigma$ 捕捉了變量間的隱含線性約束（例如物理定律導致的相關性）。
3. **優化問題 (MAP)**：
   - 目標函數：$J(c) = F(c) + \kappa \sum \omega_i (c - c^R_i)^T \Sigma^\dagger (c - c^R_i)$。
   - 第一項 $F(c)$ 是成本函數（如燃油消耗）。
   - 第二項是正則化項（Mahalanobis Distance），強迫解 $c^*$ 靠近參考軌跡的加權平均，從而間接滿足未知的物理約束。

• **PyRotor 庫**：

- 實現了上述流程，包含數據讀取、約束定義、優化求解器接口。

---

### 📊 結果與討論 (Results & Discussion)

• **主要發現 (Primary Results)**：

1. **航空案例 (Aircraft Climb)**：
   - 優化飛機爬升軌跡以減少燃油。
   - 在沒有顯式氣動力學公式的情況下，僅通過歷史飛行數據，PyRotor 找到了比平均參考軌跡更省油的解，且保持了飛行特徵。
2. **帆船案例 (Sailing)**：
   - 最大化力場做功。
   - 證明了框架在非線性力場下的有效性。

• **圖表摘要**：

- 文中展示了通過 $\kappa$（懲罰係數）調節，可以在「低成本」與「高真實度（符合約束）」之間取得平衡。

---

### 🧠 深度評析 (Synthesis & Critique)

• **核心貢獻**：

- 提供了一種非侵入式（Non-intrusive）、無模型（Model-Free）的軌跡優化方法。
- 巧妙利用協方差矩陣的零空間（Null Space）來捕捉隱含約束，是一個亮點。

• **邏輯一致性**：

- 理論推導完整，從無限維空間映射到有限維，再到貝葉斯推斷，邏輯閉環。

• **對 HFT 的啟示**：

- HFT 中的 **Optimal Execution (VWAP/TWAP)** 本質上也是軌跡優化問題。
- 我們通常假設衝擊模型（Market Impact Model）。如果衝擊模型未知，是否可以用 PyRotor 這種思路，基於歷史成交軌跡（Reference Executions）來優化每一筆單子的路徑？
- 將 "Execution Schedule" 視為軌跡，"Cost" 為滑點，"Constraints" 為庫存限制。

---

### 📝 寫作語料庫 (Citable Material)

• **定義 (Projected Trajectory)**: "A trajectory y is decomposed on a finite number of basis functions... trading the initial infinite dimension problem for a parameter optimisation problem."

---

### 🚀 行動清單 (Action Items)

- [ ] **探索 PyRotor**：查看 GitHub (如果有) 或自行實現核心的 MAP 優化邏輯。
- [ ] **應用於 Execution Algo**：嘗試用此框架設計一個「無模型」的執行算法，僅基於歷史 Level 3 數據中的最優執行路徑進行學習。
