---
name: clonoth-ryza-tts-ops
description: Ryza ONNX TTS 完整运维记录：hybrid 环境修复、部署包文档、性能与显存实测、空闲卸载、CUDA context 残留
enabled: true
strategy: normal
keywords:
- ryza
- onnxruntime
- hybrid
- idle_unload
- 空闲卸载
- 显存
- VRAM
- CUDA context
- local_onnx_tts
- TTS 性能
- 部署包
priority: 75
scan_depth: 1
---

# Ryza ONNX TTS 运维记录（完整版）

> 记忆里只留摘要，技术细节以本 skill 为准。

## 1. hybrid 模式（已上线 2026-10-06）

`config/runtime.yaml` 的 `local_onnx_tts_provider = "hybrid"`。

component_providers 分配：bert / t2s_encoder / t2s_first / t2s_step 走 `CUDAExecutionProvider`；sovits_decoder 走 `CPUExecutionProvider`。

### 性能实测（同文本，输出 5.02s 音频）

| provider | 稳态耗时 | RTF |
|---|---|---|
| cpu | 5.346s | 1.065 |
| hybrid | 3.619s | 0.721 |

加速约 **1.48x**。差异全在模型部分（CPU 3.968s → GPU 2.411s），解码器两边都约 1.18s。

### 修通时踩的两处坑

1. **onnxruntime 目录坏混合态**：`onnxruntime.dll` 是 CPU 版 1.22.0，覆盖了 gpu 版 1.21.1 → `get_available_providers()` 只报 CPU。修复：`pip install --force-reinstall --no-deps onnxruntime-gpu==1.21.1`（对应 CUDA 12.x + cuDNN 9，匹配本机 CUDA v12.4 运行时 DLL）。修后 providers = `['TensorrtExecutionProvider','CUDAExecutionProvider','CPUExecutionProvider']`。
2. **`supervisor/local_onnx_tts.py` 的 `_preload_cuda_dlls()` 版本检查过严**：原仅接受 cuda 11.x + cudnn 8，torch 2.6.0+cu126 / CUDA 12.6 / cuDNN 9.5 直接抛 RuntimeError。修复：放宽为按 major 接受 CUDA 11.x/12.x + cuDNN 8/9。

### 隐患

环境里 `onnxruntime-directml 1.22.0` 仍在，与 onnxruntime-gpu 同包名，将来若被重装可能再次覆盖 CUDA provider。

## 2. 部署包文档同步

部署包：`../BotTTS/ryza_v2proplus_onnx_deploy`（**工作区外**）。

原文档硬性固定「PyTorch + cu118」单轨，与实际环境不符。改成按 `python_version` 双轨：

- `<3.12` → onnxruntime-gpu 1.19.2（CUDA 11 / cuDNN 8）
- `>=3.12` → onnxruntime-gpu 1.21.1（CUDA 12 / cuDNN 9）

涉及文件：`requirements-gpu.txt`、`start_hybrid_windows.ps1`（补提示）、`README.md`（补 Clonoth 实测）、`部署说明.md`（第 6 节改写 + 新增 6.2）、`docs/BENCHMARKS_ZH.md`。

## 3. 空闲卸载（已上线）

配置：`config/runtime.yaml` 的 `local_onnx_tts_idle_unload_seconds: 30`（0 或负数 = 永久常驻）。

`supervisor/local_onnx_tts.py` 实现要点：

- `LocalOnnxTtsConfig` 加字段 `idle_unload_seconds`（默认 30.0）
- `LocalOnnxTts` 加 `_last_used` / `_active_calls` / `_idle_timer`
- `_schedule_idle_unload_locked()`：`threading.Timer`（daemon）重新计时
- `_release_if_idle()`：空闲到时 `self._engine = None` + `gc.collect()`；有活跃请求时重新计时
- `synthesize` 的 `_run()` 用 `try/finally` 维护 `_active_calls` 与 `_last_used`
- `config_from_mapping` 读配置时**用显式 None 判断**，避免 0 被 `or` 吞掉

### 实测

单次请求：t=0 GPU 0 MiB（未加载）→ 合成峰值 4296 MiB → 约 30 秒后回落 1142 MiB。

## 4. CUDA context 残留（重要）

空闲卸载释放的是 ONNX 引擎对象（合成峰值 → 回落 1142 MiB，省约 **3.1 GB**），但 **ONNX Runtime 的 CUDA context 一旦初始化就不归还**，这部分常驻约 1.1 GB。

所以显存**无法**回到「重启后、从未跑过 TTS」的 0 MiB 状态。

> 澄清：GPU 占用与 Discord 适配器无关 —— dcbot 进程不在 GPU 进程列表里。占用来自 supervisor，因为 TTS 跑在 supervisor 进程内（`app.state.local_onnx_tts`）。

要彻底回收需把 TTS 拆成**独立子进程**（空闲即杀）。属较大改动，需用户拍板。

## 5. 重载速度实测

测法：同文本连发两次，加载开销 =（wall1 - synth1）-（wall2 - synth2），自动抵消 HTTP 与锁开销。

| 场景 | 加载开销 |
|---|---|
| 空闲卸载后重载（CUDA context 仍在） | ≈3.6s |
| 引擎仍驻留（距上次请求 <30s） | ≈0.2s |
| 全新进程冷启动 | ≈4.6s |

重载比彻底重启快约 1 秒（差异主要来自 CUDA context 初始化）。

附带观察：同一文本首次合成比第二次慢约 0.3s，疑似 cuDNN 首次 kernel 选择预热。

## 6. 显存 vs 文本长度实测

方法：走线上 HTTP 用 supervisor 已有引擎（**绝不叠实例**）；`nvidia-smi -lms 100` 长效采样抓峰值；先热身摘掉加载开销。

基线（引擎已加载）4298 MiB：

| 字数 | fragments | 最大 tokens | 音频 | 峰值 | 相对基线 |
|---|---|---|---|---|---|
| 11 | 1 | 52 | 2.38s | 4298 MiB | +0 |
| 449 | 21 | 323 | 90.06s | 4810 MiB | +512 |

结论：

1. **不会爆显存**。最坏情况（449 字，接近 510 上限）峰值 4810 MiB，8GB 卡剩约 3.3GB。
2. 长文本抬高峰值但温和：+512 MiB（+12%）。
3. 归因未完全确认：「单 fragment token 数」还是「fragment 数量累积」，尚未区分。

对 ASMR 设计的启示：分段推理省显存收益有限；分段的真正价值在失败隔离、进度可见、时间轴精确。

## 7. 重要连带影响

TTS hybrid 常驻约 4.4GB 显存，8GB 卡剩约 3.7GB；Stable Audio Open 需 6GB+，**两者无法在同一张 8GB 卡上同时常驻** → 触发音生成需错峰或另备机器。

## 8. 相关脚本

**位置：`data/backups/scripts/`**（不在自动清理区；原先放在 `data/temp/` 会被 24h 清掉）

| 脚本 | 用途 |
|---|---|
| `test_hybrid_engine.py` | 单进程引擎自检 |
| `bench_cpu_vs_hybrid.py` | cpu vs hybrid 性能对比 |
| `verify_hybrid_live.py` | 走线上 HTTP 验证 hybrid 生效 |
| `probe_vram_vs_length.py` | 显存 vs 文本长度（第 6 节数据的来源） |
| `probe_rate_and_determinism.py` | 语速与确定性实测 |
| `verify_idle_unload.py` / `verify_idle_live.py` | 空闲卸载验证 |
| `audit_memory.py` / `audit_inject2.py` | 记忆与注入量审计 |
| `find_split.py` | 定位引擎切分正则 |

运行方式：从工作区根目录执行（脚本内用绝对路径常量，不受 CWD 影响）。

> 另有记忆维护脚本在 `data/backups/scripts/`；如需回滚记忆，备份在 `data/backups/memory_backup_20261006_0415/`。
