# ASMR 触发音素材获取方案

> ⚠️ **本项目是独立工程，不属于 Clonoth/Bot。**
> 本文档只是调研与设计参考，**不要在 Clonoth 工作区里实现**素材管理、生成、混音等任何工具链，也不要在 bot 节点里挂载相关能力。实现代码放在独立仓库/目录中。

> 整理日期：2026-10-06
> 适用场景：ASMR 音频内容制作的触发音（trigger sound）采集与生成

---

## 这个文档在讲什么（人话版）

ASMR 里那些「翻书声」「耳语」「雨声」统称**触发音**。一期节目要用到几十上百个，所以得先解决：**上哪弄，能不能商用。**

一句话结论：

- **主力**：Pixabay + Freesound —— 品类齐全，均有 API 可脚本化检索，授权可商用
- **补充**：DLsite 同人音声包 —— 人头麦质感最好，但**实测品类极窄**（几乎只有掏耳/梵天类）
- **兜底**：Stable Audio Open 本地生成（材质类可以，耳语/口腔音不行）
- **授权铁律**：只有 CC0 或明确写「可商用」的才能用

下面开始是详细调研。

---

## 0. 为什么不能用通用音效库

ASMR 触发音与游戏/影视音效在需求上基本是两类东西：

| 维度 | 通用音效 | ASMR 触发音 |
|---|---|---|
| 听感位置 | 声音在「场景里」发生 | 声音在「耳道里」发生 |
| 房间反射 | 需要（体现空间） | 污染（必须极干） |
| 声场 | 单声道/普通立体声 | 双耳定位，左右耳独立 |
| 素材时长 | 一次性 | 需支撑 20–30 分钟循环 |
| 录制方式 | 常规话筒 | 人头麦（dummy head） |

所以 Sonniss GDC、Kenney 这类通用库虽然音质专业、授权宽松，但默认带距离感和空间感，**不对口 ASMR**。真正对口的是日本同人音声圈那套生态。

---

## 1. DLsite 同人音声素材集（最对口）

ASMR 触发音的「原产地」。特点：KU100 人头麦录制 + 专门为耳かき/梵天这类内容制作。

| 社团 | 内容 | 授权 | 备注 |
|---|---|---|---|
| **みんなの効果音** | 梵天 vol.1–10，KU100 收录 | 营利/非营利均可，**无需署名**，可改 | 单卷便宜，系列化 |
| **SPGroup** | 癒しの耳かきバイノーラル効果音素材集 | **著作権フリー**，商用可，可加工 | 耳かき棒/綿棒/梵天/耳タオル |
| **SweeTec** | 【効果音】梵天音 ASMR | **商用OK，署名不要** | 总长 7:50 |
| **ばっくすくらいちゃー** | パンパン効果音 224 个 | 商用可，无需署名 | KU100，48k/96k 双规格，含位置对照表 |
| **シュミノサウンズ** | 耳かき効果音素材集（RJ104199） | 商用可，**需在 readme 署名** | 含 mono/立体声/MP3 三版 |

**价格区间**：单个约 100–1000 日元。

**共通限制**：
- 禁止二次配布素材本身（打包进成品可以）
- 少数要求署名（如シュミノサウンズ）

**结论**：性价比最高的打底来源。购入 2–3 个免署名包（みんなの効果音 + SPGroup）即可覆盖主要类别。

---

## 2. 免费音效站

### Pixabay Sound Effects（主力）

- 搜 `asmr` 有 654 条结果，授权为 Pixabay Content License（免署名，可商用）
- 推荐创作者：**babydolldaydreams** —— Plastic Tapping、Wet Mouth Sounds、Binaural Brushing、Crumpled Plastic、Ribbon Box Tapping、Cardboard Box Tapping 等，覆盖面广
- 优点：免登录浏览、免署名、免费

**API 情况（2026-10-06 用真实 key 实测）**

| 端点 | 结果 |
|---|---|
| `/api/`（图片） | HTTP 200，正常 |
| `/api/videos/` | 正常 |
| `/api/audio/` | **HTTP 403 Access denied** |

**结论：Pixabay 公开 API 不支持音效检索。**

判断依据三条：

1. 官方文档只列 images / videos
2. 第三方文章明确指出「music、sound effects、gifs 不在 API 内」
3. 真实 key 请求 `/api/audio/` 返回 **403**（端点存在但无权访问）

> 修正：上一版曾据「无效 key 返回 400」推断该端点可用 —— 那是**鉴权层**的报错。换成有效 key 后，错误变成**权限层**的 403，说明它不对外开放。

### ✅ 解法：Pixabay 素材大多能回溯到 Freesound

**关键发现（2026-10-06）**：Pixabay 上有个账号 **`freesound_community`**（2024-10-24 注册），**77,039 条音效**，全部是从 Freesound 搬运的 **CC0** 素材。

它的页面会**标注原出处**：

```
Needle Drop
  greenvwbeetle (Freesound)      ← 原作者
  Surface-noise, Crackle, Pop sound effect.
  freesound_community
```

**所以流程可以变成**：

```
1. 在 Pixabay 网页上听到好素材
2. 看它是否标了 "xxx (Freesound)"
   ├─ 标了 → 去 Freesound 找原作者 → 用 --original 下【原始质量】
   └─ 没标 → Pixabay 原生素材 → 只能网页手动下
```

**实测验证**（4 个案例）：

| Pixabay 条目 | 标注的原作者 | Freesound 定位 |
|---|---|---|
| Coin drop | MPTSound | ✅ 命中 |
| Metal Impact | j1987 | ✅ 命中 |
| Fairy_sound | AdriMB86 | ✅ 命中 |
| Needle Drop | greenvwbeetle | ⚠️ 搜索词需调整 |

**大部分能回溯，且拿到的是一手原始质量**——比 Pixabay 的下载还干净。

### ⚠️ Pixabay 原生创作者：多为 MP3，且查无 Freesound

**`babydolldaydreams`** 这类只在 Pixabay 发的创作者，有两个问题：

**① Freesound 上查无此人**（404）→ 无法回溯

**② 格式多为 MP3**（用户 2026-10-06 一手观察）：

```
Plastic Tapping ASMR  |  babydolldaydreams
  Saves 46   Media type: MP3   Published: Jan 30, 2025
```

**推测机制**（未验证）：虽然官方 FAQ 说允许上传 MP3/WAV/AAC/FLAC/AIF/M4A，但**分发给用户时可能统一转成 MP3**。站内那个「wav 分类」（约 1372 条）相对总量而言占比极小。

**后果**：这部分素材拿到的是 MP3，高频细节有损。而 ASMR 触发音最讲究的「沙沙」「叮」恰好落在易损的高频区。

**验证手段**：素材下回来后，用 `data/backups/scripts/analyze_audio.py` 做频谱分析——看高频截止在哪。128kbps MP3 通常 16kHz 就切，320kbps 能到 20kHz。

### ⚠️ 无法全自动

Pixabay 网页对爬虫返回 **403**，所以**没法自动读取页面上的出处标注**。得人工看一眼。

不过这也符合 Pixabay 的条款——他们明确禁止批量自动化。

**顺带发现**：图片端点返回字段里有 `isAiGenerated` 和 `noAiTraining` —— Pixabay 的 API 结构自带 AI 标记字段。「标的来源与 AI 标记」这个需求，在 Pixabay 这边可以靠官方字段满足。

**已知限制**：

| 限制 | 说明 |
|---|---|
| 速率 | 100 请求 / 60 秒 |
| **强制缓存** | 响应必须缓存 24 小时 |
| **禁止批量下载** | 明确写了「Systematic mass downloads are not allowed」 |
| API 只给元数据 | 返回的是 URL，音频文件还要再下载一次 |
| 不可再分发 | 不能把音效单独打包发布（嵌进成品可以） |
| 不可训 AI | Pixabay 内容（含 AI 生成）**不能用于训练模型** |

### Freesound（主力）

- 总量 73 万+，可按 license 过滤，**CC0 部分**可商用免署名
- 有专门的 ASMR pack，例如 Elias79 的 ASMR pack（海绵摩擦、指甲敲玻璃、耳朵清洁）
- 技巧参考：**salemaudio 的 ASMR Mic Foam Scratching** —— 作者建议叠两轨、时间错开、一轨 90% L 一轨 90% R，**90% 比 100% 更「近」**

**API 情况（官方 APIv2）**

| 项 | 说明 |
|---|---|
| 申请 | https://freesound.org/apiv2/apply |
| 认证 | `token` GET 参数 或 `Authorization: Token {KEY}` 头 |
| Python 客户端 | `freesound-python`（官方维护） |
| 速率 | 检索 60/分、2000/天；下载类 30/分、500/天 |

### 申请凭据怎么填

创建 API credentials 表单四个字段：

| 字段 | 填什么 | 必填 |
|---|---|---|
| **Name** | 应用名，随便起，如 `asmr-sound-collector` | ✅ |
| **URL** | 项目主页；没有就留空 | ❌ |
| **Callback URL** | 本地脚本收不到回调 → 填 Freesound 指定地址（见下） | ❌ |
| **Description** | 用途描述，如 "collect ASMR trigger sounds for a personal audio project" | ✅ |

**Callback URL 是关键**。表单说明里已经写明：如果应用无法处理回调请求（本地脚本、非 web、非服务器类都算），要填这个固定地址：

```
http://freesound.org/home/app_permissions/permission_granted/
```

填了它，OAuth2 授权完成后 `code` 会**显示在网页上**，手动复制回来即可，不需要起本地服务器。

**创建后页面给两个值**：

| 值 | 用途 |
|---|---|
| **Client ID** | OAuth2 用 |
| **Client secret / Api key** | 简单检索时拿它当 token；OAuth2 也用它当 secret |

> 只做「搜索 + 下载预览」的话，**用 Client secret 当 token 就够了，不必走 OAuth2**。只有下载原始质量才需要 OAuth2。

搜索示例：

```
GET https://freesound.org/apiv2/search/?query=asmr&token={KEY}
```

**支持按授权过滤**（这个很实用）：

```
filter=license:"Creative Commons 0"
```

**⚠️ 关键限制：普通 token 只能拿「预览版」**

| 要什么 | 怎么拿 | 质量 |
|---|---|---|
| 预览版 | 普通 token 即可 | mp3 ~128kbps / ogg ~192kbps |
| **原始质量** | **必须走 OAuth2** | 上传时的原格式（wav/flac/aiff） |

**这对 ASMR 是个实际问题**：128kbps mp3 的高频通常在 16kHz 附近就被截断了，而 ASMR 触发音最讲究的「沙沙」「叮」恰好落在这个区间。**要拿高质量素材，绕不开 OAuth2。**

### OAuth2（2026-10-06 已跑通）

授权一次即可，之后 refresh token 自动续期。工具：`data/backups/scripts/freesound_oauth.py`

```
setup --client-id X --client-secret Y    # 存应用凭据
        authorize                        # 生成授权链接
exchange "CODE"                          # 用授权码换 token（拿原始质量必需）
token / status                           # 取用 / 查看
```

**实测结果**（id=614080 翻页声）：

| 项 | 预览版 | **原始质量** |
|---|---|---|
| 格式 | mp3 128kbps | **wav 44.1kHz 立体声** |
| 大小 | 430 KB（4.8s） | 114 KB（0.7s） |

**踩过的坑**：OAuth2 的 access token **必须放 `Authorization: Bearer` 头**，不能像普通 api key 那样放 `token` query 参数——放错返回 401。

**下载端点限速更严**：原始质量下载是 30/分、500/天（检索是 60/分、2000/天）。

### ❌ Soundpaint Free ASMR（排除）
虽然内容质量高（社区众筹，8GB+/800+ 条，耳语到陶土摩擦），但素材挂在 Soundpaint 自家采样器里，**需要安装其插件才能使用**。不适合做可分发的工作流，故排除。

---

## 3. AI 生成触发音

### 结论：可行，但有明确边界

| 模型 | 输出 | 显存 | 模型大小 | 授权 |
|---|---|---|---|---|
| **Stable Audio Open 1.0** | 立体声 44.1kHz，最长 47s | ~8GB | 1.3B（autoencoder 156M + T5 109M + DiT 1057M） | 权重开放，CC 数据训练 |
| **AudioLDM 2** | 单声道 16kHz（48k 变体 있음），约 30s | ~6GB | 346M / 712M | 开放，但训练含 AudioSet（授权不明） |

**关键技术点：**

1. **Stable Audio Open 是拿 Freesound 数据训的**（+ Free Music Archive），所以对「材质类」声音理解很好 —— 敲击、摩擦、纸、织物、水
2. 8 步/秒 @ RTX 3090（24GB），消费级显卡可跑
3. 支持 **text-to-audio** 和 **audio-to-audio（风格迁移）**
4. 可本地微调（论文提到 2×A6000 可微调）

### 推荐 prompt 写法

**核心原则：写材质，不写声音。** 给出材质，模型能推声音；只写声音，模型得猜材质。

```
close-mic slow knife cutting through dense kinetic sand,
soft granular crumble, dry room, no reverb, no music
```

关键 modifier：
- `close-mic, intimate` —— 拉近距离
- `dry room, minimal room tone, no reverb` —— 去空间感
- `no music, no speech, no whisper` —— 排除不需要的元素
- 负面提示固定加：`low quality, distorted, noise, static`

**参数甜点区**（Stable Audio Open）：
- `num_inference_steps`: 100
- `guidance_scale`: 6.0–8.0（超过 10 出现 artifacts）
- 每个触发音**生成 6–8 个变体**，挑瞬态最干净的 2 个

### ⚠️ 已知边界（重要）

生成音频模型有普遍短板，**不只在这些模型上成立**：

| 类别 | 表现 |
|---|---|
| 材质类触发音（玻璃、织物、食物、纸、水） | ✅ 站得住 |
| **贴耳耳语、口腔音** | ❌ 最弱一档 |

所以：
- **耳语人声** → 交给自建 Ryza Whisper TTS，别指望生成模型
- **材质触发音** → 可以用生成模型补缺
- 生成结果通常**空间感偏「远」**，需后期 EQ 处理（100Hz 低切 + 6kHz 轻提）才能贴近

### 进阶：微调专属生成器

Stable Audio Open 权重开放、体量可控，可以拿收集到的触发音素材做**领域微调**，训练一个专属的 ASMR 触发音生成器。这是长期最有价值的方向 —— 能持续产出多样性且独家的素材。

---

## 3.5 硬件配置需求

### 模型构成

1.3B 参数总量，拆开看：

| 组件 | 参数量 | 说明 |
|---|---|---|
| Autoencoder | 156M | 波形 ↔ latent 编解码 |
| T5-base 文本编码器 | 109M | 文本条件 |
| DiT 扩散主干 | 1057M | 主要算力消耗 |

### 关键瓶颈：解码器，不是扩散主干

论文附录 B 的实测数据：

| 阶段 | 显存占用 |
|---|---|
| DiT 扩散过程 | 5.9 GB |
| **latent 解码回波形** | **14.5 GB** |

解码阶段显存需求是扩散阶段的 2.5 倍。**低显存卡必须开 chunk decoding**（把 latent 序列切成带重叠的块分别解码再拼接），重叠区包含解码器感受野（每侧 16 个 latent）时音频结果完全一致。

### 硬件分档

| 档位 | 显卡 | 显存 | 100 步耗时 | 8 个变体 | 备注 |
|---|---|---|---|---|---|
| **最低可跑** | RTX 4060 / 3060 | 8–12GB | 25–40s | 4–6 min | 必须 FP16 + chunk decoding（**本机 4060 8GB 属此档**） |
| **舒适** | RTX 4060 Ti 16G / 4070 | 12–16GB | 15–25s | 2–3 min | 无需特殊优化 |
| **推荐拐点** | RTX 3090 / 4090 | 24GB | 8–12.5s | 1.5–2 min | 官方基准点 |
| **极速** | A6000 / A100 | 48–80GB | 5–9s | <1 min | 生产级 |

**官方基准**：论文 5.4 节给出 RTX-3090 8 steps/sec、RTX-A6000 11 steps/sec、H100 20 steps/sec。

**vLLM 实测**：RTX 4090 24GB 跑 10 秒音频，离线与在线生成的峰值显存均约 12.6 GiB。

### 重要特性：耗时与音频长度无关

模型使用固定 47 秒的生成窗口（靠 timing 条件填充静音，再裁掉尾部）。**所以生成 10 秒和生成 47 秒的耗时基本相同**，耗时只跟扩散步数挂钩。这意味着长触发音不会额外增加成本。

### 加速手段

| 手段 | 效果 | 代价 |
|---|---|---|
| **TeaCache** | 1.5–2.0× | 训练无关；锐利瞬态（玻璃、敲击）建议用 TangoFlux 系数，氛围类用 Identity 系数 |
| **降低步数** | 100 → 50 约省一半 | 质量损失轻微；低于 50 会发闷 |
| **FP16** | 显存减半 | 基本无损 |
| `torch.compile` | 视情况 10–30% | 首次编译耗时 |

**TeaCache 实测**（Tesla T4，50 步，5 秒音频，`Glass bottle shattering`）：

| 配置 | 耗时 | 加速比 |
|---|---|---|
| 基线（无缓存） | 15.32s | — |
| 阈值 0.2 | 14.77s | 1.04× |
| 阈值 0.6 | 8.67s | **1.77×** |
| 阈值 0.8 | 8.43s | 1.82× |

### 批量生产量级估算

以一条 25 分钟 ASMR 为例，按每分钟 5 个触发音、每个生成 7 个变体计算：

- 总触发音数：约 125 个
- 总生成次数：约 875 次
- RTX 3090（12.5s/次）：约 **3 小时**
- RTX 4060（30s/次）：约 **7.3 小时**
- 开 TeaCache（1.77×）后 3090 可压到约 **1.7 小时**

**结论**：偶尔补缺用 8GB 卡就够；要做批量生产，24GB 是性价比拐点。

### 微调需求（若要训专属模型）

论文附录 C 提到：改用 20 秒窗口微调用 **2× RTX-A6000（48GB）**；改用 11 秒窗口微调用 **1–2× A6000**。微调门槛明显高于推理。

### 与现有 TTS 共存（本机 RTX 4060 8GB 实测）

**当前状态**：`config/runtime.yaml` 的 `local_onnx_tts_provider` 已设为 **`hybrid`**，TTS 模型部分走 GPU，解码器留 CPU。

Ryza ONNX TTS 支持三种 provider 模式：

| 模式 | 模型 | 解码器 | 说明 |
|---|---|---|---|
| `cpu` | CPU | CPU | 不占显存，最慢 |
| `hybrid` | CUDA | CPU | 占少量显存，**当前配置** |
| `cuda` | CUDA | CUDA | 占显存最多 |

**`hybrid` 模式的实现**（见 `supervisor/local_onnx_tts.py` 的 `_providers()`）：模型部分走 CUDA，解码器部分走 CPU，以此降低显存峰值。和 Stable Audio Open「解码器吃显存」的处理思路上属于同一类优化。

**实测对照**（RTX 4060 Laptop 8GB，同一段中文文本，输出 5.02s 音频，取稳态值）：

| provider | 总耗时 | RTF | 走 GPU 的组件 |
|---|---|---|---|
| `cpu` | 5.346s | 1.065 | 无 |
| **`hybrid`** | **3.619s** | **0.721** | bert / t2s_encoder / t2s_first / t2s_step |

**加速约 1.48×**，RTF 从 1.065 降到 0.721（合成速度已超过实时）。解码器在两种模式下耗时基本一致（约 1.18s），差异全部来自模型部分：CPU 3.968s → GPU 2.411s。

**线上验证**（重启后实测）：`component_providers` 显示 bert / t2s_encoder / t2s_first / t2s_step 四项走 CUDA，`sovits_decoder` 走 CPU，符合 hybrid 设计。GPU 显存总占用约 **4430 MiB**（含系统其他进程），故 8GB 卡上 TTS hybrid 常驻后剩余显存约 3.5–4GB。

**⚠️ 环境适配记录（2026-10-06 修复）**

部署包基准要求是 PyTorch cu118 / CUDA 11.8 / cuDNN 8.7 / onnxruntime-gpu 1.19.2，而 Clonoth 的 Python 3.12 环境已升级到 torch 2.6.0+cu126 / CUDA 12.6 / cuDNN 9.5。直接切 hybrid 会撞上两处问题，均已修复：

**问题 1：代码版本检查过严**
`_preload_cuda_dlls()` 原本硬性要求 `cuda.startswith("11.") and cudnn.startswith("8")`，CUDA 12.6 直接触发 `RuntimeError`。
→ 已放宽为同时接受 **CUDA 11.x + cuDNN 8** 与 **CUDA 12.x + cuDNN 9** 两组组合。

**问题 2：onnxruntime 目录是坏的混合状态**
原目录里 `onnxruntime.dll` 是 CPU 版 1.22.0，`onnxruntime_providers_cuda.dll` 是 gpu 版 1.21.1 残留，导致 `get_available_providers()` 只报 CPU。
→ 已用 `pip install --force-reinstall --no-deps onnxruntime-gpu==1.21.1` 修复。该版本对应 CUDA 12.x + cuDNN 9，与本机 CUDA 12.4 运行时 DLL（cudart64_12 / cublas64_12 / cudnn64_9 等全套齐全）匹配。修复后 provider 列表变为 `['TensorrtExecutionProvider', 'CUDAExecutionProvider', 'CPUExecutionProvider']`。

**注意**：`onnxruntime-directml 1.22.0` 仍在环境中，与 onnxruntime-gpu 同包名，将来若被重新安装可能再次覆盖。

**回滚**：修改前的 onnxruntime 目录已备份到 `data/temp/ort_backup/`（约 362MB，temp 目录会定期清理）。如需恢复原状，重装 `onnxruntime==1.22.0` 即可。

**部署结论**：
- TTS 走 `hybrid` → 模型部分占约 4GB 显存，8GB 卡剩余约 3.5–4GB
- Stable Audio Open 需 6GB+（FP16 + chunk decoding）→ **两者无法在同一张 8GB 卡上同时常驻**
- 实际做法：触发音生成任务与 TTS 错峰运行，或给生成模型单独准备机器

**如果将来想换 `cuda` 模式**：解码器也上 GPU，速度还能再提升，但显存占用显著上升，8GB 卡基本不够。当前 `hybrid` 是这张卡上的合理选择。

---

## 4. 自录方案（当前暂缓）

ASMR 圈行业标准是 **3Dio Free Space**（$399）：硅胶人耳 + 耳道内嵌麦克风。

**反常识点**：3Dio 入门款 FS **比** 旗舰 FS Pro II **更适合录 ASMR**：

| | Free Space | Free Space Pro II |
|---|---|---|
| Sensitivity | -28 dB | -34 dB |
| **SNR** | **80 dB** | 71 dB |
| Max SPL | 122 dB | 134 dB |
| 价格 | $399 | $1,999 |

FS 更灵敏、本底噪声更低；Pro II 的高 SPL 上限只在录飞机起飞这类场景才用得上。**录小声内容，FS 反而更干净**，还便宜五倍。

**预算替代**：耳塞式双耳麦（如 B1-E 人头麦替代品），精度差些但比通用大振膜麦贴耳效果好得多。

---

## 5. 组合建议

考虑到是**自用 + 对外只发工作流框架**：

1. **主力**：Pixabay + Freesound —— API 检索 + 按需下载（见下节策略）
2. **补充**：DLsite 素材包 —— 只在需要「人头麦掏耳质感」时购入；不必作为打底
3. **兜底**：Stable Audio Open 本地生成，补材质类缺口；长期可微调专属模型
4. **人声层**：走自建 Ryza Whisper TTS，不依赖生成模型
5. **发布时只发流程**：素材文件不发，让使用者自行去对应渠道获取，同时避开二次配布授权问题

---

## 6. 授权避坑

**可以用**（免署名 / 商用友好）：
- DLsite 标注「商用可・クレジット不要」的素材包
- Pixabay（自有协议）
- Freesound 的 **CC0** 部分
- Stable Audio Open 生成结果

**要署名**：
- Freesound 的 CC-BY
- DLsite 中要求标注的社团（如 シュミノサウンズ）

**不能用**：
- Freesound 的 CC-BY-NC（非商用）
- BBC Sound Effects（RemArc，非商用）
- Sonniss GDC（**禁止用于 AI/ML 训练**；仅作混音素材可用）

**核心原则**：CC0 或明确 Royalty-Free 才能闭眼用；「免费下载」从来不等于「免费发布」。

---

## 7. 后期处理清单（配套）

触发音素材到手后，还需处理：

1. **补底噪**：垫 -48 ~ -52dB 房间底噪（绝对数字静音是最破坏沉浸感的破绽）
2. **EQ 贴近感**：100Hz 以下低切 + 6kHz 左右 +2dB
3. **立体声编排**：耳语 L60 / 触发音 R60，每 2–3 分钟交换左右
4. **响度对齐** + de-esser

---

## 8. 制作流程实测数据（2026-10-06）

用本机 Ryza hybrid 实测，用于规划 ASMR 制作流水线。

### 8.1 语速基准

| 文本长度 | 输出音频 | 逐字语速 |
|---|---|---|
| 159 字 | 30.68 s | 311 字/分 |
| 135 字 | 26.80 s | 302 字/分 |
| 449 字 | 90.06 s | 299 字/分 |

**取值约 300 字/分钟**（normal_cn 预设）。

### 8.2 确定性（关键）

同一文本 + 同一 seed，**跨请求逐字节一致**：

- 两次请求 SHA-256 完全相同（`f8e37f…`）
- 音频时长完全一致
- **更强的一点**：先合成「今天天气不错。」，再合成「今天天气不错。我们出去走走吧。」，后者开头的字节与前者**完全一致**，semantic token 也对齐（`[27]` vs `[27, 36]`）

原理：引擎对每个 fragment 使用 `seed + index` 独立采样。因此**片段级别的合成结果可复现、可拼接** —— 意味着可以零散生成再合并，不必一次性跑完整篇。

### 8.3 拼接安全性

三个样本的片段首尾 50ms 峰值：

| 样本 | head peak | tail peak |
|---|---|---|
| S1 | 4 | 0 |
| S2 | 4 | 0 |
| LONG | 4 | 0 |

（int16 满量程 32767）

**片段首尾基本是静音**，直接拼接不会爆音。

### 8.4 切分行为与性能

- 449 字按 `sentence` 策略切成 **21 个 fragment**（按标点切）
- 引擎在 fragment 之间自动插入 **0.3 s 静音**（`fragment_interval` 默认值，上限 2.0 s）
- 推理/音频时长比 **0.65**（比实时快约 1.5 倍）
- 449 字 → 90.06 s 音频，推理 58.54 s

### 8.5 30 分钟节目的量级估算

| 说话占比 | 台词字数 | 请求次数（510 字/次上限） |
|---|---|---|
| 40% | 3590 | 7.0 |
| 50% | 4487 | 8.8 |
| 60% | 5384 | 10.6 |

### 8.6 对流程设计的三个结论

1. **长停顿必须由拼接阶段插入**。引擎句间静音上限只有 2.0 s，ASMR 需要的 3–10 s 停顿无法在 TTS 内生成。
2. **切分点等于标点写法**。引擎按 `。！？` 等自动切分，台本里的标点直接决定切分位置，`max_fragment_characters`（默认 240）是二级约束。
3. **两阶段流程可行**：先出全部 TTS 拿实测时长 → 用实测时长构建精确时间轴 → 在时间轴上叠加触发音。因为合成确定性，这个时间轴可复现。

---

## 参考链接

- DLsite: https://www.dlsite.com/
- Pixabay Sound Effects: https://pixabay.com/sound-effects/search/asmr/
- Freesound: https://freesound.org/
- Stable Audio Open: https://huggingface.co/stabilityai/stable-audio-open-1.0
- AudioLDM 2: https://github.com/haoheliu/audioldm2
