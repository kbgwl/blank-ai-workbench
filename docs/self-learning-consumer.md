# 消费级软件怎么"从人的行为里学"：机制、体感，与可抄的三条

> 日期：2026-11-19
> 范围：只回答一个问题——**消费级/办公类软件是怎样让"从用户行为中学习"这件事被用户接受的**。要机制，不要功能清单。
> 证据口径：优先官方帮助中心、官方博客、官方技术文档、可复现的论文。厂商不公开的地方直接写"不公开"。
> 事实与推测分开：带 `【事实】` 的是文档/论文能对上的；带 `【推断】` 的是我从机制推出来的，没有厂商背书。

---

## 0. 结论先行

### 0.1 一句话

**消费级软件的自学习能被接受，靠的不是"学得准"，而是"学得可见、可改、可停"——把模型状态做成用户能读、能删的条目，把负反馈做成能落到"这一类"的动作，把隐式窥探压到最低、把显式"我教你"抬到最高。**

### 0.2 最值得 agent 抄的三条

| # | 机制 | 消费级原型 | 为什么对 agent 关键 |
|---|---|---|---|
| **1** | **记忆条目化，而不是开关化** | Copilot 的 Saved memories 列表、Notion 的 Memories 数据库、Spotify 的 Taste Profile 笔记 | 用户能"看见一条、改一条、删一条"。开关只能整体投降，条目才能局部纠错。**且删除必须真删到派生推断**——Copilot 官方写明"关掉记忆不会删已存记忆""30 天内重开会把删掉的记忆加回来"，这是反面教材 |
| **2** | **负反馈分层：这条 / 这类 / 全部** | YouTube：不感兴趣 → 说明原因 → 不要推荐这个频道 → 清空全部反馈；TikTok：不感兴趣 → 关键词过滤 → 主题滑块 → 刷新 feed | agent 的"经验学歪"从来不是单条错，而是**一类任务反复套同一个错套路**。只支持"删这一条"，用户会陷入打地鼠 |
| **3** | **默认少学，学的时候让用户看见"这是你教我的"** | 钉钉：必须用触发指令"帮我记住我的职务是 CIO"，最多 10 条；IME：选词即强化、长按即删除；Netflix：把"星级"换成"拇指"，评价量涨 200% | 隐式信号（停留、跳过、改动幅度）噪声大且用户没打算表态。把写入通道收窄到显式动作，记忆精度和用户信任同时上升 |

### 0.3 一条反直觉的发现

**"被接受"是界面语义问题，不是算法问题。**

Netflix 把五星评分换成拇指时，官方口径是：用户把"星级"理解成"给大众打平均分"，把"拇指"理解成"教系统认识我的口味"，于是同样的评价动作，**评价量涨了 200%**（[Netflix, 2017](https://about.netflix.com/news/goodbye-stars-hello-thumbs)）。

【推断】这说明用户反感的不是"被学习"，而是"我做的动作被拿去做了我没同意的解释"。改一个图标、加一句"因为你看了 X"，接受度就能翻倍——这对 agent 的措辞和交互位置的启示，比换模型大。

---

## 1. 对照表

> "官方未公布"= 查过官方文档/博客，没有给出该数字或机制，不是我没找到。

| 产品 | 信号 | 生效速度 | 可见性 | 撤销方式 | 防学歪 |
|---|---|---|---|---|---|
| **微软拼音** | 隐式：上屏词、词频；可关"动态词频调整" | 【推断】即时（本地词库） | 候选排序变化；无"为什么是这个候选" | 设置→词库和自学习→清除输入历史；关自学习；隐私→墨迹书写和键入个性化→清除 | 只有"清空重来"，无分类纠错 |
| **搜狗拼音** | 隐式：词频、上屏；云输入须弹窗授权（整改后） | 官方未公布 | 候选排序；无解释 | 关云同步；切"基础打字模式"（不收集任何数据，代价是皮肤/表情/语音/翻译消失） | 无；2021 年因违规收集被下架，整改后才补上模式选择 |
| **iOS 键盘** | 隐式：**拒绝某次自动更正本身就是"教"**（Apple 官方描述） | 【推断】即时，端侧 | 候选栏/行内灰字 | 单条：部分版本可长按候选移除（非官方保证）；整体：设置→通用→传输或还原→还原键盘字典 | 只有整体重置；错误拼写会被反复强化 |
| **Gboard** | 隐式：点没点建议、上屏词；联邦学习默认开 | 端侧即时；全局模型迭代周期未公布 | 候选排序 | 隐私→删除已学词和数据；关"个性化服务"；单独关联邦学习 | 分层开关（个性化 / 联邦学习 / 语音捐赠各自独立） |
| **SwiftKey** | 隐式：词、词间搭配、**按键落点偏移** | 端侧即时 | 候选栏；长按可删单词/"不再预测" | 长按候选删除；设置→输入→清除输入数据；data.swiftkey.com 删云端 | 单条可删是最细粒度；但**预测栏不能关**（官方明确） |
| **Gmail 智能撰写** | 隐式：你实际怎么写信；个性化默认开 | 官方未公布 | 建议直接出现在正文，Tab 接受 | 设置→智能撰写个性化→关；账号级生效 | 关闭后回到通用建议；有反馈表单 |
| **Gmail 重要邮件标记** | 隐式：收件人互动频率、打开、回复、常读关键词、星标/归档/删除 | 论文：每用户模型 **T_min < 24h** 更新；培训材料：批量纠错"几分钟"、发件人级"一天左右" | 悬停黄色标记即显示"为什么判为重要" | 点标记改判（同时就是训练信号）；设置里可"不使用我过去的操作记录预判" | 可整体退出个性化，保留手动规则 |
| **YouTube** | 隐式：观看时长、完播、点赞、订阅；显式：不感兴趣/不喜欢/已看过 | 官方未公布 | "不感兴趣→说明原因"；基于相似用户的推荐会显示解释框 | 不感兴趣 / 不要推荐此频道 / 我的活动里删除全部反馈 / 关观看历史（首页直接无推荐流） | 主题探索、Shorts 减量、可完全关闭推荐 |
| **TikTok** | 隐式：**观看时长权重高于其他因子**（官方 DSA 报告）、跳过、完播；显式：不感兴趣 | 官方未公布；刷新 feed 后"重新学习" | "Why this post"（分享菜单）列出影响因素 | 不感兴趣 / 关键词过滤 / 主题滑块 / 刷新 feed / 欧盟可切非个性化流 | 排序阶段有"相似度检查"主动替换相似内容；主题滑块只能降频不能清零 |
| **Spotify** | 隐式：搜索、收听、**跳过**、收藏；显式：Taste Profile 里写笔记 | **排除播放列表 48 小时内生效；Discover Weekly 可能要到下周**；Taste Profile 笔记"几小时内" | Taste Profile 直接给你看"Spotify 眼里你的口味"摘要，可读可改 | 排除播放列表 / 编辑或删除笔记 / 隐藏歌曲 | 编辑策展 + 算法混合；官方称"推荐不该只为下一次点击优化" |
| **Netflix** | 隐式：观看历史、完播、时段、设备；显式：拇指 | 官方未公布 | % Match 百分比（明确说是"对你的预测"，不是全站评分） | 拇指向下即从首页移除；可批量改判 | 官方声明**不使用年龄/性别等人口统计学特征** |
| **Copilot Memory** | 隐式：对话推断 + M365 工作数据 + 使用信号；显式："请记住…" | 即时（弹出"Memory updated"） | 保存时弹提示；设置里有条目列表 | 单条删除 / 全部删除 / 关 Saved memories / 关 Chat history personalization（删推断） | **几乎没有**：管理员不能限制写入内容类型；无审计日志；Purview 保留策略不适用 |
| **Notion Agent** | 隐式：对话中提到的角色、项目、常开页面、纠正 | 下一次对话 | Settings→Notion AI→Memory，可打开 Memories 数据库逐条看 | 编辑/删除任一条；关 Use memories；工作区 owner 可整体关 | 默认不从其他页面/评论/连接应用挖；Custom Agent 默认关闭记忆 |
| **飞书 aily** | 隐式："每日自动抽取工作记忆"；显式：一句话设定偏好 | 官方称"每日"抽取 | "个性化配置"里可见；具体条目界面未公开 | 官方帮助未给出逐条删除入口（见 §5） | 未公开 |
| **钉钉 AI 助理** | **只认显式触发指令**："帮我记住我的职务是 CIO" | 下一次对话 | 助理详情页→我的记忆 | 只能在详情页改，**不支持对话里改** | 上限 10 条自定义变量——用容量硬约束防膨胀 |
| **Obsidian** | 不是学习：链接、提及就是全部信号 | 即时（保存即索引） | 反向链接面板：已链接提及 + **未链接提及**，点一下变成链接 | 删掉那段文字就删掉了这条"关系" | 不存在"学歪"：每条边都能指回原文段落 |
| **Google 搜索** | 隐式：搜索历史、点击、位置、语言、设备 | 官方未公布 | **单条结果标"Personalized for you"**；页脚标"个性化/非个性化"，可点"尝试不使用个性化功能" | 关 Show personal results；关 Web & App Activity | 官方声明不推断种族/宗教/政治归属，且"Search 不会把人放进过滤气泡"（厂商主张） |

---

## 2. 逐对象：五个问题

### 2.1 输入法

#### 信号从哪来

【事实】四家都**没有"点赞"式的显式反馈**；所有信号都埋在正常打字动作里。但关键在于：**每个信号都对应一个用户实际做过的手势**——上屏了哪个词、下一个词是什么、有没有按退格改掉。

- **iOS**：Apple 官方对学习机制的描述很直白——**"你拒绝 iPhone 建议的某个词，就等于把这个词加进了键盘词典"**。每一次驳回自动更正，都在告诉手机"我这个拼法是有意的"（[Apple 用户指南口径，经 iphonewired 转述](https://iphonewired.com/jailbreak/1200314/)）。这是最纯粹的"负反馈即训练"。
- **SwiftKey**：除了词和词间搭配，还**学习按键落点**——"it also adapts to the positions of your keystrokes as you type, helping it decide what you mean if you tap the area between two keys"（[官方 FAQ](https://support.microsoft.com/en-us/topic/d737059d-8810-448e-b376-9af56171a37d?shownav=false)）。这是键盘独有的信号：它连你手指偏了 3 毫米都知道。
- **Gboard**：记录"建议出现时的上下文 + 你有没有点它"，在设备上做联邦学习（[Google Research, 2017](https://research.google/blog/federated-learning-collaborative-machine-learning-without-centralized-training-data/)）。
- **微软拼音**：本地词频 + 可选云候选；"动态词频调整"是可关闭的独立开关（[Microsoft Learn 社区答复](https://learn.microsoft.com/zh-cn/answers/questions/3147660/edge)、[另一条](https://learn.microsoft.com/zh-cn/answers/a/8029952)）。

#### 多快生效

【推断】**下一次按键**。因为词库在本地、打分在本地，选中一次生僻词，下一次输入同音就该往前排。这是所有消费级学习系统里反馈回路最短的——不到 2 秒。

#### 用户怎么知道它在学

**它不解释，但它把学习结果直接摆在用户正在看的地方。** 候选词列表就是模型的输出。用户不需要"记忆管理界面"来理解模型学到了什么——排序变了，就是学到了。

【推断】这是输入法最特殊的一点：**模型的内部状态和产品的可见输出是同一个东西**。推荐系统做不到（推荐流是模型输出，但用户不知道"为什么"），agent 也做不到（记忆在系统提示词里，用户看不见）。

#### 用户怎么撤销

四家都能整体清空，但只有前三家有"单条"这一层，而且只有它们的粒度对得上用户能感知到的东西：

| 层级 | iOS | Gboard | SwiftKey | 微软拼音 |
|---|---|---|---|---|
| 单条 | 部分版本长按候选移除；官方只保证整体重置 | 词典→删除已学词 | **长按候选→删除 / 不再预测**（官方文档明确） | 无 |
| 全部 | 还原键盘字典（需密码确认） | 删除已学词和数据（需输入确认码） | 清除输入数据；云端另删 | 清除输入历史 |

【事实】SwiftKey 的官方文档把这件事写得最细：长按候选词 → 弹窗 → OK，这个词就不再被预测；官方给的适用场景是"你打错了，或者键盘学到了你想让它藏起来的东西"（[Prediction Bar 官方说明](https://support.microsoft.com/en-us/swiftkey-keyboard/how-does-the-microsoft-swiftkey-prediction-bar-work)）。

【事实】**但 SwiftKey 同时明确：预测栏不能关、不能隐藏**（"Predictions are an integral part of the Microsoft SwiftKey experience, therefore the prediction bar cannot be turned off"）。这是一个很好的边界样本：**"能删一条"不等于"能退出"**。

#### 学歪了会怎样

输入法的"学歪"是最原始、最可见的一种：**错误拼写被当成有意的，然后被反复强化**。Apple 那边的典型症状是"越用越 autocorrect 成那个错词"，唯一的解是整体重置。

【事实】重置的代价被官方写明了：自定义词、文本替换全部删除，头几天自动更正会明显变笨，然后重新学（[MacMyths 整理 Apple 口径](https://macmyths.com/how-to-clear-keyboard-history-on-iphone/)）。

【推断】输入法没有"防学歪"机制，只有"推倒重来"。它能被容忍，是因为**重置的代价 ≈ 几十秒**，而且损失是可预期的（就是变笨几天）。这条对 agent 极其重要：**如果重置记忆的代价是一周，用户就永远不敢重置，也就永远无法纠错。**

---

### 2.2 邮件

#### 信号从哪来

两个完全不同的东西常被混为一谈：

**（a）重要性排序（Priority Inbox）——纯隐式。** 官方列的信号是：你和谁通信、通信频率、你打开哪些邮件、回复哪些、你常读的邮件里出现的关键词、你星标/归档/删除了哪些（[Gmail 帮助](https://support.google.com/mail/bin/answer.py?hl=zh-Hant&answer=186535)）。

Google 自己的论文把目标函数写得很清楚：**不是"这封邮件重要"，而是"用户在看到它之后 T 秒内会不会对它做动作"**（打开、回复、手动纠正都算），并且这个 T_min 被限制在 24 小时以内——因为要保证用户"有机会看到"（[The Learning Behind Gmail Priority Inbox](https://research.google.com/pubs/archive/36955.pdf)）。

**（b）写作风格（Smart Compose 个性化）——隐式，但对象是"你怎么写"。** 官方说"会根据您通常的写作方式提供个性化建议，让您保持一贯的行文风格"，且"只有您自己可以看到"（[Gmail 帮助](https://support.google.com/mail/answer/9116836?hl=zh-hans)）。

#### 多快生效

- 重要性：论文写明每用户模型 **T_min < 24 小时**更新一次；Google 的培训材料另外给了两个更细的口径——**批量纠正后"几分钟内"重要/不重要邮件总量就会变化；持续纠正某个发件人，"一天左右"会变成永远重要/永远不重要**（[Google Gmail 教学文档](https://docs.google.com/a/blakeschool.org/document/pub?id=18rdcKQQ2EW0Ro230VzyxmglLPFHAGCcpjjaXI055rCU)）。这条材料是 Google 自己写的培训稿，不是论文，标注来源。
- 写作风格：官方未公布。

#### 用户怎么知道它在学

**这是 Gmail 最值得抄的一处：它把"为什么"做成了一个 hover。**

【事实】把鼠标移到黄色重要标记上，就能看到系统把这封邮件标为重要的原因（[Gmail 帮助](https://support.google.com/mail/bin/answer.py?hl=zh-Hant&answer=186535)）。

不是"我们的 AI 判断这封重要"，是**具体到"因为你经常回复这个发件人"**。可解释性在这里不是合规装饰，是让用户敢于点那个标记去纠正的前提——**如果用户不知道为什么，他改的时候就是在赌博**。

#### 用户怎么撤销

- 单条：点一下重要标记，立刻改判，**这个动作同时就是训练信号**（"这也会帮助 Gmail 了解哪些邮件对你来说是重要的"）。
- 全局：设置→收件箱→"不使用我过去的操作记录预判哪些是重要邮件"。
- 写作风格：设置→智能撰写个性化→关闭，账号级生效。

【推断】注意这里的精巧之处：**纠正和训练是同一个按钮**。用户不需要理解"我在提供训练数据"，他只是在说"这封不重要"。学习是纠错的副产品，不是额外任务。

#### 学歪了会怎样

【推断】邮件的"学歪"表现为**重要性膨胀或塌缩**：一旦你把某类邮件都标为不重要，系统可能再也捞不出来。但因为纠正成本是一次点击、且可以整体退回手动规则，实际危害被限制住了。

【事实】Gmail 还有个"逃生舱"：可以在设置里让某些发件人"始终/从不"标为重要，用确定性规则覆盖统计模型（[Gmail 官方博客, 2010](https://gmail.googleblog.com/2010/09/5-tips-for-using-priority-inbox.html)）。**给用户一个"别学了，按我说的来"的硬规则通道**，这比任何解释都管用。

---

### 2.3 推荐系统

#### 信号从哪来

**隐式压倒性主导，显式是补丁。**

- **TikTok** 官方在 DSA 报告里写了一句罕见的具体话：**"对多数用户，观看某个视频的时长权重高于其他因子"**（[TikTok DSA Report](https://disinfocode.eu/reports/tiktok/8?commitmentId=362&chapterId=74)）。官方 Creator Academy 更直白："如果你不假思索地划走一段 ASMR 视频，那么推荐页就会减少推送 ASMR 内容"（[TikTok Creator Academy](https://www.tiktok.com/creator-academy/zh-Hans/article/guidelines-recommendation-system-intro)）。
- **Spotify** 把信号集合命名成一个可命名的对象：搜索、收听、**跳过**、收藏 → "taste profile"（[官方说明](https://www.spotify.com/us/safetyandprivacy/understanding-recommendations)）。
- **Netflix** 官方列的是：观看历史、你对其他片的评分、相似口味用户、内容元数据（类型/演员/年份）、**观看时段、语言、设备、看完没看完**；并明确**不含年龄性别**（[Netflix 帮助](https://help.netflix.com/en/node/100639)）。
- **YouTube** 在 DSA 透明度文档里列了订阅、点赞、**点踩**、不感兴趣、不要推荐此频道、满意度问卷——并特别说明**问卷是为了理解"满意度"而不只是观看时长**（[Transparency Center](https://www.disinfocode.eu/reports/youtube/5/text)）。

#### 多快生效

**全部"官方未公布"，只有一个例外。**

【事实】Spotify 是唯一给出明确延迟的：**把某个播放列表排除出 taste profile，48 小时内生效；但 Discover Weekly 这类每周生成的推荐可能要等到下周**（[Spotify 帮助](https://support.spotify.com/au/article/your-taste-profile/)）。

这个数字很有价值：它承认了**"个性化"和"周期性产物"是两条不同步的链路**。用户点了"别用这个列表影响我"，下周才看到变化——如果不提前说清楚，用户会认为"点了没用"。

其余：YouTube、TikTok、Netflix 均不公开。TikTok 只说"刷新 feed 后，系统会根据你对新视频的互动重新学习"。

#### 用户怎么知道它在学

这是过去五年变化最大的地方，三家都做了"为什么给我看这个"：

| 产品 | 入口 | 展示什么 |
|---|---|---|
| YouTube | 推荐位下方小info框 | "基于其他相似兴趣用户看过/喜欢的内容"（[官方博客, 2018](https://blog.youtube/news-and-events/giving-you-more-control-over-homepage)） |
| TikTok | 分享 → Why this post | 列出具体因素：你赞过/分享过/评论过类似内容、该内容在你所在国家的流行度、你偏好更长视频、你关注了作者（[DSA 报告](https://disinfocode.eu/reports/tiktok/8?commitmentId=362&chapterId=74)） |
| Netflix | 无 | 只有 % Match 这个数字，没有"为什么" |

【事实】Netflix 明确说过 % Match 的含义："这个百分比是对**你**可能喜欢这部片的预测，完全基于算法分析你的个人观看习惯，**不是全站受欢迎程度**"（[官方](https://about.netflix.com/news/goodbye-stars-hello-thumbs)）。它解释了"这个数是什么"，但没解释"为什么是这个数"。

【推断】三种可见性层次：**解释数字含义（Netflix）< 解释单个结果的原因（YouTube）< 列出具体因素清单（TikTok）**。做到第三层，用户才有可能做"针对性纠正"——否则他只能整体不信任。

#### 用户怎么撤销

撤销被拆成了**不同粒度**，这点比"一键关闭"成熟得多：

- **单条**：不感兴趣 → 还要选原因（已看过 / 不喜欢），这是把"负反馈"变成"分类标签"。
- **一类**：不要推荐这个频道（YouTube）；关键词/话题标签过滤、主题滑块（TikTok）。
- **整体**：清空全部"不感兴趣"反馈（YouTube 我的活动 → 其他活动 → 删除）；刷新 feed（TikTok）。
- **退出**：关闭观看历史 → YouTube 首页直接不显示推荐流，只剩搜索框和导航（[DSA 文档](https://www.disinfocode.eu/reports/youtube/5/text)）；欧盟用户可切非个性化流（DSA 第 38 条）。

【事实】TikTok 的"刷新 feed"官方措辞很克制：它**不删除你的账号数据、不删除关注**，只是改变推荐（[TikTok 说明](https://www.tiktok.com/creator-academy/zh-Hans/article/guidelines-recommendation-system-intro)）。这种"说清楚它不做什么"的表述，本身就是降低反感的手段。

#### 学歪了会怎样

见 §4。这里只说机制层面的一件事：

【事实】TikTok 在排序阶段有一道"相似度检查"——"我们希望你的推荐页内容新鲜有趣，因此系统会**替换相似内容，确保推荐内容的多样性**"（[Creator Academy](https://www.tiktok.com/creator-academy/zh-Hans/article/guidelines-recommendation-system-intro)）。TikTok 为此牺牲了一部分即时点击率。

【推断】这是唯一一个把"防窄化"写进排序流水线的公开案例：不是等用户点了"不感兴趣"才去多样化，而是在出结果前就强制去重。对手动纠错依赖越少的系统，越需要这种"结构性防腐"。

---

### 2.4 办公套件

#### 信号从哪来

四家的路线差别极大，正好构成一条从"全自动"到"全手动"的光谱：

| 产品 | 学到什么 | 怎么进来 |
|---|---|---|
| **Copilot Memory** | 沟通风格、常用主题、工作目标、重复任务 | **自动**：从对话里推断 + M365 工作数据 + 使用信号 + 档案信息；也支持"请记住…" |
| **Notion Agent** | 角色、团队、常合作的人、在做的项目、常用页面、你在对话里给的纠正 | **自动**，但**只从对话**——官方明确"目前不会从你的其他页面、评论或连接的应用建立记忆" |
| **飞书 aily** | 工作记忆、偏好、说话风格 | **每日自动抽取**（官方："系统每日自动抽取工作记忆"） |
| **钉钉 AI 助理** | 岗位、主管、职务等 key-value 变量 | **只认显式触发指令**："帮我记住我的职务是：CIO"；不支持语义识别自动赋值；上限 **10 条** |

【事实】Copilot 的写入原则官方说法是"只在有明确记忆意图时才保存"：说"我所有数据科学任务都用 Python"→记住；说"用 Python 写个 k-means"→不记（[官方博客](https://techcommunity.microsoft.com/blog/microsoft365copilotblog/introducing-copilot-memory-a-more-productive-and-personalized-ai-for-the-way-you/4432059)）。

【推断】这条原则听起来很好，但它和"默认开启 + 从对话推断"是矛盾的：**判断"有没有明确意图"的人是模型，不是用户**。钉钉的做法虽然笨（必须说触发词、只能 10 条），但**写入的决定权 100% 在用户手里**，这是两种完全不同的信任模型。

#### 多快生效

- Copilot：**即时**，保存时弹一个"Memory updated"提示，下一次对话就用上。
- Notion：下一次对话。
- 钉钉：下一次对话；首次激活时整理历史数据"一般需要 1 小时左右，最晚明天一早"（[钉钉个人助理文档](https://docs.dingtalk.com/i/p/Qrkoz3py2breX6L73RmQyaDnxbp31maP)）。
- 飞书：官方说"每日"抽取，具体延迟未公布。

#### 用户怎么知道它在学

三种完全不同的做法：

1. **事件提示**（Copilot）：保存那一刻弹"Memory updated"。优点是有即时感，缺点是**它一闪就没，用户不会去核对内容**。
2. **条目面板**（Copilot / Notion）：设置里能看到每一条，Notion 甚至把它做成一个数据库（"打开你的 Memories 数据库"）。这是最可审计的形态。
3. **无提示**（钉钉 / 飞书）：钉钉的记忆要自己进"助理详情页→我的记忆"看；飞书只有"个性化配置"这个笼统说法，**没找到逐条查看的公开文档**（见 §5）。

【推断】**"弹提示"和"能核对"必须同时存在**。只有前者，用户会有"它偷偷记了什么"的焦虑；只有后者，用户永远不会主动去看。Copilot 两者都有，是这一项做得最完整的；也是它后面那些坑最刺眼的原因（见下）。

#### 用户怎么撤销

**这一节是整份报告里最重要的负面案例。**

【事实】Copilot 官方文档明确写了四件事：

1. **关掉 Copilot Memory 不会删除已保存的记忆**——"Turning off Copilot Memory doesn't automatically delete already saved memories. If you want to delete memories, you have to delete them one by one or use the Delete all memories button"（[官方](https://support.microsoft.com/en-us/microsoft-365-copilot/manage-copilot-memory-in-microsoft-365-copilot)）。
2. **关闭"对话历史个性化"会删除推断，但 30 天内重新打开会把删掉的记忆加回来**（[官方](https://support.microsoft.com/en-us/microsoft-365-copilot/personalize-what-microsoft-365-copilot-remembers)）。
3. **企业 Purview 的保留策略不适用于 Copilot memory**；管理员不能限制"什么类型的信息可以写入"；记忆操作**不产生审计日志**（[Microsoft Learn](https://learn.microsoft.com/zh-tw/copilot/microsoft-365/copilot-personalization-memory)）。
4. 记忆存在**用户 Exchange 邮箱的一个隐藏文件夹**里，因此继承邮箱的安全与合规策略，但管理员只能用 eDiscovery/Graph 去搜和删。

【推断】这四条合起来是一个很危险的组合：**用户以为"关了"，数据还在；用户以为"删了"，30 天内可能复活；管理员既不能限定写入范围，也拿不到审计线索。** 这不是技术问题，是**"删除"这个动作的语义被稀释了**。一旦用户发现"删了还在"，之后所有的记忆功能他都不会再信。

对比 Notion：官方写的是"打开你的 Memories，编辑或删除任何你不想让 agent 保留的内容"，且"Memories 对每个用户私有"（[官方](https://www.notion.com/sv/help/view-your-notion-agent-memories)）。它没说"关掉会怎样"——这本身是个缺口，但至少没承诺一个假的删除。

#### 学歪了会怎样

【事实】办公套件的记忆学歪，主要不是"推窄"，而是**把错的当成永久的**：

- Copilot 的记忆会"合并相关的、更新过时的、在你要求时删除"——**合并是模型做的，用户不一定知道合并后还剩什么**。
- 钉钉的记忆变量**不支持在对话里改**，只能去详情页改；也就是说，如果你在聊天里说"其实我现在是 VP 了"，它听到了但改不了。
- 飞书/钉钉的记忆上限（10 条）是一种朴素的防腐：**容量小到不会形成"画像"**。

【推断】办公场景的风险和推荐场景相反：推荐怕"越推越窄"，办公怕"**越记越像一份人事档案**"。所以办公套件的正确设计目标不是多样性，而是**最小化 + 可审计 + 到期即忘**。Copilot 三条都反着来。

---

### 2.5 知识管理（Obsidian / Roam / Logseq）

这一节不是 AI，但它是"结构从行为里长出来"的最干净样本，而且**它没有隐私问题**——这两件事是同一件事。

#### 信号从哪来

**只有一种：你写了什么，以及你把两个词放在同一个句子里。**

【事实】Obsidian 的反向链接面板分两栏：

- **已链接提及**：别的笔记里有 `[[本笔记]]` 这样的显式链接。
- **未链接提及**：别的笔记里**出现了本笔记的标题，但没有做成链接**。悬停会出现"Link"按钮，点一下就把那段文字包上双链（[Obsidian 官方文档](https://obsidian.md/help/backlinks)）。

【事实】官方对"未链接提及"的定位写得很准：它是"用来发现你写作时没意识到的联系"。

#### 多快生效

保存即索引，即时。没有"训练"，没有"模型更新"。

#### 用户怎么知道它在学

**它不"学"，它"显示"。** 用户看到的是"这三篇笔记提到了你现在写的这篇"，而不是"我们推断你可能对 X 感兴趣"。

#### 用户怎么撤销

**删掉那段文字，那条关系就没了。** 没有独立的"记忆库"需要清理，没有"关掉学习"的开关，因为**结构就是文本本身**。

【推断】这是最彻底的可撤销性：**证据和结论是同一个东西**。你不需要信任系统"删干净了"，因为你能看见那行字不在了。

#### 学歪了会怎样

不存在"学歪"，只存在"你写错了"。这是它和所有统计学习系统的根本区别：**Obsidian 没有归纳，只有索引。**

【推断】这正是 agent 记忆最该借的一点：**如果一条"经验"能写成"某次任务里，因为这行代码/这条报错，所以那样改"，它就自带了证据指针；如果写成"用户偏好简洁回复"，它就已经是一次不可审计的归纳了。**

---

### 2.6 搜索

#### 信号从哪来

【事实】Google 官方说个性化会用到：位置、过去的搜索历史、搜索设置；并举例"在芝加哥搜 football 会看到美式足球和芝加哥熊队，在伦敦会看到英式足球和英超"（[Google 搜索的工作方式](https://g.co/kgs/v8hXATC)）。它同时声明**不推断种族、宗教、政治归属**这类敏感特征。

#### 多快生效

官方未公布。

#### 用户怎么知道它在学

**这是所有产品里最克制的做法，也最值得抄：它按条标注。**

【事实】如果某一条结果是为你个性化的，"About this result"面板里会出现一句 **"Personalized for you"**；如果没有这句，就说明这条没被个性化（[官方帮助](https://support.google.com/websearch/answer/12410098)）。

另外，结果页页脚会标"个性化搜索结果 / 非个性化搜索结果"，并给一个链接"**尝试不使用个性化功能**"——点它只看这一次，不改设置（[官方帮助](https://support.google.com/websearch/answer/12412910?hl=zh-US)）。

【推断】"试一次不带个性化"这个按钮是整个调研里最聪明的设计之一：**它把"关掉"从一次性决策变成了零成本实验**。用户不需要在"便利"和"隐私"之间做永久选择，他可以先看看差别有多大。大多数时候差别很小——这反而增强了信任。

#### 用户怎么撤销

- 单次：页脚"尝试不使用个性化功能"。
- 全局：关 Show personal results；关 Web & App Activity（会连带关掉个性化）。
- 【事实】官方明确说明"关掉个性化不会删除已存的活动记录"，删除要去数据与隐私控制里单独做（同上）。这条和 Copilot 的问题一样：**"不学" ≠ "忘掉"**，但 Google 至少把它写在同一个帮助页里。

#### 学歪了会怎样

【事实】Google 的官方立场是强否认："Google Search 不会把人放进过滤气泡"，理由是系统不推断敏感特征，且每次个性化都会告知（[g.co](https://g.co/kgs/v8hXATC)）。

【推断】这是**厂商主张，不是可验证结论**。可验证的只有：它给每条结果打了标、给了退出按钮、给了"试一次"的通道。至于排序有没有系统性窄化，外部无法审计——因为排名函数不公开。这一点在 §4 会展开。

---

## 3. 为什么输入法学习打字习惯被接受，而别的软件做同样的事会被觉得被监视

先纠正问题的前提。

**不是"输入法被接受"，是"某种形态的输入法学习被接受"。** 2021 年 5 月，网信办通报 33 款 App 违法违规收集个人信息，讯飞、搜狗、QQ 输入法等被点名；6 月多款输入法被下架（[人民网评论](https://opinion.people.com.cn/n1/2021/0616/c431649-32132196.html)、[贝壳财经实测](https://news.qq.com/rain/a/20210613A05B0I00)）。整改之后，搜狗给出"完全体验模式 / 基础打字模式"二选一——**基础模式不收集任何数据，代价是皮肤、表情、语音、翻译消失**；讯飞把基础输入模式做成完全离线（[人民日报客户端](https://www.peopleapp.com/rmharticle/30051188375)）。

所以真正的问题应该是：**满足哪些条件的输入法学习被接受？** 我找到六个差异，按解释力排序。

### 3.1 收益和证据在同一屏、同一秒（解释力最强）

输入法的学习结果就是候选栏。学对了，你少按两次键；学错了，你立刻看见，按一下退格。**预测、检验、纠正三步全在 2 秒内完成，且全在用户视线里。**

推荐系统做不到：推荐错了，你可能三天后才发现"怎么全是这类"；agent 也做不到：记忆用错了，你要等到任务失败才知道是哪条记忆害的。

【推断】用户能容忍一个"每次都当面接受检验"的系统，因为**他始终在裁判席上**。他不能容忍一个"背着他归纳，然后在别处用出来"的系统——哪怕后者准确率更高。

### 3.2 学的是词，不是事实（语义带宽极低）

一个词频表能表达"这个人常打'甲状腺'"，但不能表达"这个人有甲状腺疾病"。

【事实】Apple 处理这件事的方式很说明问题：它用**本地差分隐私**去发现"新词"，数据在设备上就被随机化，服务器永远看不到原始记录；而且这是**用户主动选择加入**的分析共享，不加入就什么也不上传（[Apple "Learning with Privacy at Scale"，经 MacStories 转述](https://www.macstories.net/?p=52277)、[9to5Mac](https://9to5mac.com/?p=511040)）。

【事实】SwiftKey 的说法更直接：**"By default, nothing Microsoft SwiftKey learns about your typing leaves your device."** 只有你主动登录账号，才会把词片段同步到云端，并可在 data.swiftkey.com 要求删除（[官方 FAQ](https://support.microsoft.com/en-us/topic/d737059d-8810-448e-b376-9af56171a37d?shownav=false)）。

【推断】**"学词"和"学人"是两种不同性质的操作。** 词频表即使泄露，泄露的是"你打过这些字"；记忆条目泄露，泄露的是"你是这样的人"。前者是数据，后者是判断。用户对这两件事的容忍度天然不同——这不是双标，是**信息类型不同**（这正是 Nissenbaum 的"语境完整性 / contextual integrity"（2010）：隐私侵犯不在于收集本身，而在于信息流是否越出了它被提供时的语境）。

### 3.3 本地是默认值，不是承诺

四家的默认值都是端侧：

| 产品 | 官方表述 |
|---|---|
| SwiftKey | 默认不离开设备；登录才同步 |
| Gboard | 个性化数据存在本机；联邦学习只上传模型更新，且用 Secure Aggregation 保证服务器只能看到"几百/几千人平均后"的结果 |
| 微软拼音 | 本地词库 + 可关的云候选（两个独立开关） |
| iOS | 端侧词典；分析上报是 opt-in + 差分隐私 |

【推断】用户验证这件事的成本很低：断网打字，候选照样工作。**"我能用行为验证你的承诺"** 是信任的关键，而"请你相信我们的隐私政策"不是。

### 3.4 学习动作是用户做的，不是系统偷看的

选词 = 正强化；退格改掉 = 负强化；长按删除 = 否定。**每一个学习事件都挂着一个用户手势。**

对比推荐：你在一张图上停了 3 秒，系统就记了一笔——**你从没打算表态**。

对比 Copilot：你说"我在做 Project Alpha"，它就存了一条记忆——**你只是在陈述上下文，不是在授权**。

【推断】这是"被教"和"被观察"的分界线。同样的数据，前者用户是主体，后者用户是样本。**用户反感的不是被记录，是被解释。**

### 3.5 撤销的粒度 = 感知的粒度

用户能感知到"这个词老出来"，就能长按删掉这个词。**感知粒度和操作粒度是对齐的。**

反例是 Copilot：用户能感知的是"它好像知道我太多事"，但能操作的是"删某一条记忆"——中间隔着一层他看不见的推断。

【推断】当一个系统的内部状态无法被用户按"他能感知到的单位"切开时，撤销就会变成一种信仰行为。用户只能选择"全信"或"全关"。

### 3.6 接受是有条件的，条件被监管写死了

2021 年那次整改给出了中国市场的硬边界：**基本功能不需要个人信息**，云输入必须弹窗、必须能关、关了不能二次弹窗逼你开（[贝壳财经](https://news.qq.com/rain/a/20210613A05B0I00)）。

【事实】整改后的形态很值得注意：不是"关掉学习"，而是**"基础模式 / 完整体验模式"二选一**——把"能不能用"和"要不要被学"解耦。用户可以在不被学习的前提下继续打字，只是少了皮肤表情。

【推断】这条对 agent 的直接启示：**"不学习"不应该等于"不能用"。** 一个把记忆关掉就变残的 agent，等于在逼用户用隐私换功能。

### 3.7 一个反例，说明"接受"有多脆弱

【事实】同一个 Netflix，同样的学习，只把五星换成拇指，评价量涨 200%。官方的解释是用户终于明白"这是在教系统"，而不是"在给大众打分"。

【推断】把这条和输入法放在一起看，结论就很清楚了：**可接受性 = （收益同屏 × 粒度对齐 × 显式动作 × 本地默认 × 零成本撤销）÷ 二次利用。** 分子里任何一项塌掉，分母就会被放大。搜狗 2021 年塌的是"二次利用"——用户发现词频被拿去做广告推送，分子再大也没用。

### 3.8 这套类比哪里不能搬

必须说清楚：**agent 学的不是词，是做法**。一条"经验"的语义带宽远高于一个词频，它天然接近"判断"而不是"数据"。所以：

- **能搬**：显式写入、条目化、粒度对齐、零成本撤销、收益同屏、默认关闭。
- **不能搬**："本地处理就够了"。agent 的记忆就算全在本地，只要它归纳出了"这个用户不擅长并发"，用户照样会觉得被冒犯——因为**冒犯来自归纳，不来自传输**。

---

## 4. "学歪"的公开研究：结论比传说复杂

### 4.1 反馈回路本身：模拟层面已被证明

【事实】**Chaney, Stewart & Engelhardt (2018), RecSys** 用模拟证明：推荐系统是在"已经被推荐系统影响过的数据"上训练的，这种"算法混杂（algorithmic confounding）"会**让用户行为同质化，但不提升效用**；而且它会同时污染训练和离线评估（[arXiv:1710.11214](https://arxiv.org/abs/1710.11214)）。

这是对 agent 记忆最直接的一条：**你在"上次用了这条经验的任务"上评估"这条经验好不好"，评估本身就是被污染的。** 用得多 ≠ 有用。

### 4.2 现场证据：方向一致，量级远小于直觉

| 研究 | 样本 | 结论 |
|---|---|---|
| **Bakshy et al. 2015, Science** | 1010 万美国 Facebook 用户 | 好友网络本身高度同质；**算法排序让跨立场内容曝光再降约 15%（Science 编辑摘要口径；论文的分阶段分解另给 5%/8%，量级更小），但用户自己"点不点"造成的下降更大**。选择性接触 > 算法筛选（[Science](https://www.science.org/doi/10.1126/science.aaa1160)） |
| **Flaxman et al. 2016, POQ** | 5 万美国用户的浏览历史 | 社交媒体和搜索引擎同时做两件相反的事：**拉大**人与人之间的平均意识形态距离，但也**增加**个人接触"对立面"的机会；而且绝大多数新闻消费仍是直接访问常看的主流媒体首页。效应量"相对温和"（[POQ](https://dx.doi.org/10.1093/poq/nfw006)） |
| **Guess et al. 2018, Knight Foundation** | 文献综述 | "回音室""过滤气泡"叙事**高估了普遍性和严重性**，最多描述了少数人的状况（[白皮书 PDF](https://kf-site-production.s3.amazonaws.com/media_elements/files/000/000/133/original/Topos_KF_White-Paper_Nyhan_V1.pdf)） |
| **Nyhan et al. 2023, Nature** | 全美成年 Facebook 用户 + 23,377 人随机实验 | 中位数用户看到的内容略超一半来自同立场；**把同立场内容砍掉约 1/3，8 项预注册态度指标（情感极化、意识形态极端度、错误信念等）全部无显著变化**；但同立场内容的"参与率"反而上升——人在补偿算法（[Nature](https://doi.org/10.1038/s41586-023-06297-w)、[摘要](https://www.eurekalert.org/news-releases/996985)） |
| **Hartmann et al. 2024, 系统综述** | 129 项研究 | **分歧主要来自测量方式**：用同质性 + 计算社会科学方法的研究确认回音室；用"完整媒体接触面"的问卷/曝光研究否认它。研究高度集中在美国（[arXiv:2407.06631](https://arxiv.org/abs/2407.06631)） |

### 4.3 怎么读这堆结论

**第一，"越推越窄"在机制上成立，在量级上被夸大。** 模拟研究（Chaney）能干净地证明同质化，田野实验（Nyhan）却测不出态度后果。两者不矛盾：**同质化是真的，它导致的危害被我们想当然了。**

**第二，最反直觉的一条：减少同质内容不会改善态度。** Nyhan 的实验把同立场内容砍掉三分之一，跨立场曝光从 20.7% 升到 27.9%，态度指标一动不动；而且剩下的同立场内容被点得更多了。**算法可以改变供给，改不了需求。**

**第三，用户自己的选择比算法更能决定他看到什么**（Bakshy、Flaxman 都是这个方向）。

【推断】对 agent 记忆的直接映射：

- **"经验同质化"会真实发生**——同一套做法因为被召回得多而被召回得更多（和 OpenClaw dreaming 的问题同构：优化的是"被用到"，不是"正确"）。
- **但"加多样性"是无效解**——如果你只是往上下文里塞几条互相矛盾的经验，模型会挑它最熟的那条，就像 Nyhan 的用户照样点同立场内容。
- **有效解在供给侧的结构，不在数量**：TikTok 的"排序前强制去重"、Gmail 的"硬规则覆盖统计"、Obsidian 的"证据即条目"——都是**在生成结果之前就把错误结构掐掉**，而不是生成完再让用户纠。

---

## 5. 查不到的部分

明确列出来，避免把推测当事实。

### 5.1 输入法

- **搜狗/讯飞的官方技术文档**：词频更新频率、云同步到底传哪些字段、模型是否端侧、用户词库的保留期限——只有隐私政策和媒体实测，**没有可验证的技术说明**。本文引用的搜狗部分全部来自监管报道和厂商隐私政策转述，不是官方技术文档。
- **微软拼音"自学习"具体写什么文件、保留多久、清除是否彻底**：官方只有设置项说明，没有数据格式或生命周期文档。
- **iOS 键盘个性化是否 100% 端侧**：Apple 说差分隐私用于"发现新词"是 opt-in，但 QuickType 个性化本身是否纯端侧、端侧词典是否会随 iCloud 备份上云，**没有逐项文档**。
- **"单条删除候选词"在 iOS 上是否是官方支持的能力**：只有第三方教程提到长按移除；Apple 官方只保证"还原键盘字典"整体重置。

### 5.2 邮件

- **Smart Compose 个性化的生效延迟、训练信号粒度**（是按句子、按联系人还是按时间段）：官方只说"根据你通常的写作方式"，无量化说明。
- **Priority Inbox 的"几分钟/一天"来自 Google 的培训材料**，不是产品文档，也不是论文里的实验数据。论文只给了 T_min < 24h 这个约束。

### 5.3 推荐

- **YouTube / TikTok / Netflix 的排序权重、负反馈生效延迟**：全部不公开。TikTok 只肯说"观看时长权重更高"这一句。
- **Netflix 是否有"为什么推荐这部"的入口**：未找到官方说明。它只解释 % Match 的含义，不解释单个结果的原因。
- **"不感兴趣"到底降权多少、降多久**：三家都没说。外部只能观测到"点了之后短期内减少"，无法量化。

### 5.4 办公套件

- **飞书 aily 的记忆管理入口**：官方帮助只写"偏好会被记住并同步在个性化配置中"（[aily 帮助](https://aily.feishu.cn/hc/1u7kleqg/jsmwydi4)），**没找到逐条查看/编辑/删除的公开文档**，也没找到关闭开关的说明。
- **钉钉记忆的终端用户删除入口**：开放平台文档只写了开发者如何配置变量、"仅支持在助理详情页-我的记忆中更改"（[文档](https://open.dingtalk.com/document/ai-dev/market-assistant-configuration-memory)），**没写用户能不能整体关闭、能不能清空**。
- **Copilot Memory 在中国区/世纪互联版的行为**：本文引用的全是全球版文档，未找到区域版差异说明。

### 5.5 搜索

- **Google 个性化排序的实际影响幅度**：官方只给例子（芝加哥 vs 伦敦的 football），不给任何量化。第三方无法审计排名函数。
- **"Search 不会把人放进过滤气泡"是厂商主张**，不是可独立验证的结论。可验证的只有标注和退出机制的存在。

### 5.6 一个我没有回答的问题

**"用户主观上到底怎么感知这些机制"** —— 本文所有关于"用户觉得被监视 / 不觉得"的判断，都是从产品设计和官方文档反推的，**没有引用任何用户研究或访谈数据**。除了 Netflix 那个"换图标后评价量 +200%"的行为数据，以及 2021 年输入法下架这种宏观事件，我没有找到直接测量"用户对自学习机制的接受度"的实证研究。这是一个真实的空白，如果要做严谨结论，需要自己跑用户研究。

---

## 附：本文引用的一手来源清单

**输入法**
- SwiftKey Prediction Bar 官方说明（增删词、关闭预测栏）https://support.microsoft.com/en-us/swiftkey-keyboard/how-does-the-microsoft-swiftkey-prediction-bar-work
- SwiftKey 打字数据 FAQ（默认不出设备、云端删除）https://support.microsoft.com/en-us/topic/d737059d-8810-448e-b376-9af56171a37d?shownav=false
- Gboard 学习与隐私（联邦学习默认开、删除已学词、语音捐赠独立）https://support.google.com/gboard/answer/12373137?hl=zh-
- 联邦学习原始博客（Gboard 查询建议、Secure Aggregation）https://research.google/blog/federated-learning-collaborative-machine-learning-without-centralized-training-data/
- Apple 差分隐私（opt-in、端侧随机化、发现新词）https://www.macstories.net/?p=52277 ／ https://9to5mac.com/?p=511040
- 微软拼音自学习与清除（社区支持答复）https://learn.microsoft.com/zh-cn/answers/questions/3147660/edge
- 2021 输入法下架与整改（监管与实测）https://opinion.people.com.cn/n1/2021/0616/c431649-32132196.html ／ https://news.qq.com/rain/a/20210613A05B0I00
- 整改后的双模式（基础打字 / 完全体验）https://www.peopleapp.com/rmharticle/30051188375

**邮件**
- Gmail 智能撰写（个性化默认开、账号级开关、仅本人可见）https://support.google.com/mail/answer/9116836?hl=zh-hans
- Gmail 重要邮件标记（信号清单、悬停看原因、点击改判）https://support.google.com/mail/bin/answer.py?hl=zh-Hant&answer=186535
- Priority Inbox 论文（目标函数、T_min < 24h、全局+个人模型）https://research.google.com/pubs/archive/36955.pdf
- Priority Inbox 官方博客（硬规则覆盖、快捷键纠错）https://gmail.googleblog.com/2010/09/5-tips-for-using-priority-inbox.html

**推荐**
- YouTube 推荐管理（不感兴趣/说明原因/不要推荐频道/清空反馈/关历史）https://support.google.com/youtube/answer/6342839?hl=zh-hant
- YouTube 官方博客（"为什么推荐"解释框）https://blog.youtube/news-and-events/giving-you-more-control-over-homepage
- YouTube DSA 透明度（点踩仍是信号、满意度问卷、关闭历史后无推荐流）https://www.disinfocode.eu/reports/youtube/5/text
- TikTok Creator Academy（观看时长/跳过、相似度检查、刷新 feed）https://www.tiktok.com/creator-academy/zh-Hans/article/guidelines-recommendation-system-intro
- TikTok DSA 报告（时长权重最高、Why this video、Manage Topics、欧盟非个性化流）https://disinfocode.eu/reports/tiktok/8?commitmentId=362&chapterId=74
- Spotify 推荐原理（taste profile 的信号定义）https://www.spotify.com/us/safetyandprivacy/understanding-recommendations
- Spotify 排除播放列表（48 小时 / 次周生效）https://support.spotify.com/au/article/your-taste-profile/
- Spotify Taste Profile Beta（可读可写、几小时生效、可删笔记）https://newsroom.spotify.com/tag/product-announcement
- Netflix 星级换拇指（200% 评价增长、% Match 含义）https://about.netflix.com/news/goodbye-stars-hello-thumbs
- Netflix 推荐系统说明（信号清单、不含人口统计学）https://help.netflix.com/en/node/100639

**办公套件**
- Copilot Memory 个性化（记忆更新提示、临时会话、30 天恢复）https://support.microsoft.com/en-us/microsoft-365-copilot/personalize-what-microsoft-365-copilot-remembers
- Copilot Memory 管理（关开关不删数据、逐条/全部删除）https://support.microsoft.com/en-us/microsoft-365-copilot/manage-copilot-memory-in-microsoft-365-copilot
- Copilot 管理端（存 Exchange 隐藏文件夹、Purview 策略不适用、无审计、管理员不能限制写入类型）https://learn.microsoft.com/zh-tw/copilot/microsoft-365/copilot-personalization-memory
- Copilot Memory 发布博客（"明确意图才记"、默认开）https://techcommunity.microsoft.com/blog/microsoft365copilotblog/introducing-copilot-memory-a-more-productive-and-personalized-ai-for-the-way-you/4432059
- Notion Agent Memories（默认开、私有、可编辑删除、workspace 开关、Custom Agent 默认关）https://www.notion.com/sv/help/view-your-notion-agent-memories
- 飞书 aily 专属工作伙伴（每日自动抽取记忆、偏好写入个性化配置）https://aily.feishu.cn/hc/1u7kleqg/jsmwydi4
- 钉钉记忆配置（key-value、上限 10 条、必须触发指令、只能详情页改）https://open.dingtalk.com/document/ai-dev/market-assistant-configuration-memory
- 钉钉个人 AI 助理（首次整理约 1 小时、"记下来"显式保存）https://docs.dingtalk.com/i/p/Qrkoz3py2breX6L73RmQyaDnxbp31maP

**知识管理 / 搜索**
- Obsidian 反向链接（已链接/未链接提及、一键转链接）https://obsidian.md/help/backlinks
- Google 个性化与结果（"Personalized for you"标注、关闭个性化）https://support.google.com/websearch/answer/12410098
- Google 结果为何因人而异（页脚标注、"尝试不使用个性化功能"）https://support.google.com/websearch/answer/12412910?hl=zh-US
- Google 搜索工作方式（个性化举例、不推断敏感特征、否认过滤气泡）https://g.co/kgs/v8hXATC

**"学歪"研究**
- Chaney et al. 2018, RecSys — https://arxiv.org/abs/1710.11214
- Bakshy et al. 2015, Science — https://www.science.org/doi/10.1126/science.aaa1160
- Flaxman et al. 2016, POQ — https://dx.doi.org/10.1093/poq/nfw006
- Guess et al. 2018, Knight Foundation — https://kf-site-production.s3.amazonaws.com/media_elements/files/000/000/133/original/Topos_KF_White-Paper_Nyhan_V1.pdf
- Nyhan et al. 2023, Nature — https://doi.org/10.1038/s41586-023-06297-w
- Hartmann et al. 2024, 系统综述 — https://arxiv.org/abs/2407.06631
