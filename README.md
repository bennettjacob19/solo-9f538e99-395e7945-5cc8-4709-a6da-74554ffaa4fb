# C 编译缓存与固件静态库裁剪服务（FastAPI + GCC + Binutils）

板卡头文件更新后，固件 CI 无需整体重编：本服务把每个 `.c` 的 GCC 编译结果
（`.o`）缓存起来，只依据**源码 + 目录搜索顺序 + 实际用到的头文件内容**判断
缓存是否有效。命中缓存时**完全不启动 GCC**，直接返回上次产物。

固件发版时，CI 还需要在静态链接阶段裁掉多余的 `.a`，同时保证板卡钩子的**原
实现**不被换绑：`POST /trim` 接收 `/compile` 产出的主 `.o`、原链接命令里最
多 6 个普通 `.a`（按原顺序）以及要保留实现的钩子函数名，按 **GNU ld 裸机静态
链接**语义仿真，返回能链接成功、且所有指定钩子仍由**原文件**提供的**最短库清
单**，并列出每个钩子的真实来源。

交互式 API 文档：服务启动后访问 <http://localhost:8000/docs>（Swagger UI），
或 <http://localhost:8000/redoc>。裁剪接口的请求/响应 schema 与字段说明均在
`/docs` 中可见。

## 1. 快速开始（docker compose）

```bash
docker compose up --build
# 服务监听 http://localhost:8000，缓存持久化在 compose volume cache-data 中
```

无 Docker 时本地运行（需要本机已装 gcc）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CCACHE_CACHE_DIR` | `/data/cache` | 缓存产物目录（每个条目一个子目录） |
| `CCACHE_GCC_BIN` | `gcc` | GCC 可执行文件 |
| `CCACHE_GCC_TIMEOUT` | `30` | 单次编译超时（秒） |
| `CCACHE_NM_BIN` | `nm` | `/trim` 解析符号表用的 GNU nm |
| `CCACHE_READELF_BIN` | `readelf` | `/trim` 校验 ELF/LTO 用的 readelf |

## 2. 接口

### `POST /compile`（`multipart/form-data`）

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `source` | 是 | 一个 `.c` 源文件 |
| `groups` | 是 | 逗号分隔的头文件目录（分组）名，如 `board,arch` |
| `search_order` | 是 | 逗号分隔的目录搜索顺序，优先级从高到低，必须是 `groups` 的子集排列 |
| `defines` | 否 | 逗号/换行分隔的宏，按 `-D` 传入，如 `BOARD_A,FEATURE=2` |
| `extra_flags` | 否 | 逗号/换行分隔的额外 GCC 参数 |
| `headers` | 否 | 头文件，可多个；上传文件名必须是 `<分组名>/<头文件名>`，各目录内**平铺** |

头文件在源码中统一以**尖括号 + 文件名**包含，例如 `#include <util.h>`，
由服务按 `search_order` 生成 `-I` 搜索路径。支持任意深度的嵌套 `#include`，
也支持 `#if/#ifdef` 按宏值选择不同头文件。

**响应**

- 成功：`200`，body 为 GCC 生成的 ELF `.o`（文件名取源文件名 stem + `.o`）
- 编译失败：`422`，JSON 诊断，含 `command`、`returncode`、`stderr`、`stdout`
- 参数错误：`422`，JSON `detail`

**响应头（每个响应都带 `X-Cache`）**

| 头 | 含义 |
| --- | --- |
| `X-Cache: HIT` / `MISS` | 是否命中缓存。HIT 路径不启动 GCC |
| `X-Cache-Key` | 缓存键（源码哈希 + 搜索顺序 + 宏/参数 + GCC 版本） |
| `X-Cache-Reason` | MISS 原因：`compiled` / `compilation_failed` |
| `X-Used-Headers` | 本次编译实际（传递）用到的头文件列表 |
| `X-Compile-Ms` | 仅 MISS，GCC 实际耗时（毫秒） |

辅助端点：`GET /health`（GCC 版本、缓存目录）、`GET /stats`（命中数、命中率、
失效原因统计、条目数）。

### curl 示例

```bash
curl -D - -o main.o http://localhost:8000/compile \
  -F "source=@main.c" \
  -F "groups=board,arch" \
  -F "search_order=arch,board" \
  -F "defines=BOARD_A" \
  -F "headers=@headers/board/util.h;filename=board/util.h" \
  -F "headers=@headers/arch/plat.h;filename=arch/plat.h"
```

或直接使用 `scripts/example.sh`。

### `POST /trim`（`multipart/form-data`，固件静态库裁剪）

上传容器内 GCC 生成的**非 LTO** 产物（ET_REL 可重定位 ELF；含 `.gnu.lto_*`
节的 LTO 字节码会被拒绝）。库只能**整库成组删除**，保留的库维持原相对顺序。

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `main_object` | 是 | `/compile` 生成的主 `.o`，始终参与链接，等价原命令里的主目标 |
| `libraries` | 是 | 1～6 个普通 `.a` 静态库，**按原链接命令顺序**上传；库内成员文件名唯一 |
| `hooks` | 是 | 逗号/换行分隔的钩子函数名（C 标识符，可带 `@` 版本后缀），其实现必须来自原文件 |

**做了什么（与 GNU ld 裸机静态链接逐条对齐，`app/linker.py`）**

1. 用 `nm -P` 解析主 `.o` 与每个 `.a` 的成员符号表，用 `readelf` 校验
   ET_REL 并拒绝 LTO。
2. 按 ld 规则做静态链接仿真：输入从左到右扫描；归档只在轮到自己时访问，
   成员若定义了当前仍**强未解析**或仍是 **common（`C`）暂定分配**的符号才被
   抽取，归档内反复多趟直到无新成员；**弱未解析引用（`w`/`v`）不触发抽取**；
   已访问过的归档不再回看（向后依赖保持未解析）。
3. 符号绑定：强定义覆盖弱定义/common（**弱符号改绑**）；弱定义不覆盖强定义；
   common 互相合并并被强定义吸收；两个强定义冲突报 `multiple definition`；
   扫描结束后仍有强未解析符号即链接失败（弱未解析允许保留）。
4. 先仿真**原清单**：链接失败或某钩子在原清单中无任何定义，直接返回原因。
5. 记录每个钩子在原清单中的真实提供者（主 `.o` 或具体的
   `库名.a(成员名.o)`，含弱改绑后的最终绑定）。
6. 按库子集大小**从短到长**枚举（同长度内按原序号元组字典序，
   `itertools.combinations` 天然满足），对每个子集重新仿真，选出第一个
   “链接成功 + 每个钩子仍由原提供者提供”的子集，即为答案。

**成功响应（200）字段**

| 字段 | 含义 |
| --- | --- |
| `keep_library_indices` / `keep_libraries` | 最短保留库的原序号（从 0 起）与文件名，保持原顺序 |
| `removed_libraries` | 可整库删除的库（原顺序） |
| `hook_sources[]` | 每个钩子的 `source`（`main`/`library`）、`provider`、`binding`（`strong`/`weak`/`common`）、库序号/库名/成员名 |
| `extracted_members[]` | 最短链接下实际被抽取的归档成员 |
| `weak_unresolved[]` | 全程未找到定义的弱未解析符号（不影响裸机链接） |
| `candidates_checked` | 评估过的子集数量（含枚举过程） |

**失败响应（422）**：`detail.error_kind` 为 `link_failed`（原清单链接失败，
`message` 点名未解析/重复符号）、`hook_unresolved`（钩子在原清单无定义）或
输入校验错误（非 ELF、LTO、非 `.a`、成员重名、库数量越界、非法钩子名等）。

curl 示例：

```bash
curl -X POST http://localhost:8000/trim \
  -F "main_object=@build/main.o" \
  -F "libraries=@libplat.a" \
  -F "libraries=@libboard.a" \
  -F "libraries=@liblegacy.a" \
  -F "libraries=@libextra.a" \
  -F "hooks=board_hook"
```

或使用 `scripts/example_trim.sh`（库参数必须按原顺序，最多 6 个）。

## 3. 缓存语义（与需求逐条对应）

缓存键只包含：源文件哈希、目录搜索顺序、`defines`/额外参数、GCC 版本。
头文件内容**不进键**，改为在命中校验阶段逐条核对，从而实现：

1. **源码与搜索顺序相同，只改未用到的头文件 → 直接 HIT，不启动 GCC。**
   GCC 以 `-MMD -MP -MF` 生成依赖文件，服务据此记录**预处理器实际用到的
   所有传递依赖头文件**及其 SHA-256；未出现在依赖中的头文件任意增删改都不
   影响命中。
2. **已用头文件被删或改 → MISS。** 校验时要求每个已用头文件仍存在且
   字节级哈希一致，否则重新编译并覆盖该条目。
3. **更靠前的目录新增同名头文件 → MISS。** 元数据记录每个已用头文件当时
   解析到的目录下标；校验时若任何更靠前目录出现同名头文件（会发生遮蔽），
   即判定失效。
4. **嵌套包含与按宏选择。** 实际包含集合完全由 GCC 预处理结果决定，
   `#if defined(...)` 选到哪个头就记录哪个；`defines` 不同则缓存键不同，
   互不污染。
5. **编译失败返回诊断且不写缓存；改回原输入仍命中旧缓存。** 失败响应为
   `422` + stderr 诊断；因为失败不落盘，恢复输入后命中的是上一次成功产物。

缓存目录布局（内容寻址，对象文件不可变，可安全并发读取）：

```
<CCACHE_CACHE_DIR>/
  ├── objects/<sha256>.o        # 不可变的 GCC 产物（按内容哈希去重）
  └── <cache-key>/
        ├── meta.json           # 搜索顺序、已用头文件(目录下标/文件名/SHA-256)、宏、GCC 版本
        └── object.o -> ../objects/<sha256>.o   # 符号链接
```

## 4. 设计说明与边界

- 每次请求使用独立临时工作目录，头文件按分组平铺落盘，编译结束即清理；
  分组名与头文件名做白名单校验，拒绝路径穿越（`..`、`/`、`\`）。
- 编译以子进程超时保护（`CCACHE_GCC_TIMEOUT`），超时返回 `504`；子进程通过
  线程池执行，不阻塞事件循环。
- 并发安全：按缓存键加异步锁并 double-check，相同键的并发请求只编译一次，
  其余直接等锁后命中；不同键互不阻塞。对象文件内容寻址、永不原地改写，
  响应流式传输不会被并发重编译截断（有 12 线程并发回归测试覆盖）。
- 失败不缓存（无意义且可能放大错误）；同一缓存键重新编译成功后覆盖旧条目。
- 系统头文件（`/usr/include` 等）不在请求范围内，不参与失效校验；
  GCC 版本变化会使旧键自然失效。

### `/trim` 裁剪的设计与边界

- **不执行真实链接**：上传产物只经 `nm`/`readelf` 解析，链接结论由纯 Python
  仿真（`app/linker.py`）给出，快且确定；仿真规则全部用真实 GCC + GNU ld 2.40
  产物做过对照测试（`tests/test_linker_sim.py` 同时跑真实 `ld -r`/完整链接比对）。
- 仅覆盖裸机静态链接相关语义：归档按需抽取、归档内多趟、跨库单趟、强/弱/
  common 绑定、弱改绑、多定义与未解析诊断。不模拟动态链接、链接脚本、版本脚本
  与 LTO；LTO 产物在入口即被拒绝。
- 钩子“原实现”判定以**最终绑定到的提供者对象**为准（主 `.o` 或具体库成员），
  因此弱改绑场景下，删除提供强定义的后序库会让钩子退回前序库的弱对象，该子集
  会被正确排除。
- 最多 6 个库，子集总数 ≤ 2⁶=64，枚举成本可忽略；库以整库为删除单位，保留库
  严格保持原相对顺序；等长平局取原序号元组字典序最小（即优先删更靠后的库）。
- 每个请求使用独立临时目录落盘并在解析后立即清理；文件名只取 basename，
  库名/成员名做唯一性与扩展名校验。
- 上传产物视为不可信数据：服务只调用 binutils 只读解析，不执行其中任何代码。

## 5. 自测

```bash
pip install -r requirements.txt pytest httpx
pytest tests/ -v
```

测试覆盖（共 60 个用例，均已通过）：

- 编译缓存（15）：首次 MISS/重复 HIT、未用头变更保持 HIT、已用头修改/删除失效、
  删除后恢复仍命中、前置目录同名遮蔽、搜索顺序反转、嵌套传递依赖、宏值选头、
  语法错误诊断与旧缓存保留、参数校验、12 线程并发只编译一次、`/stats`。
- 链接仿真（18，与真实 GNU ld 对照）：链式抽取、向后依赖未解析、弱未解析不抽取、
  弱定义满足强引用、弱定义不响应 open common、main 弱定义阻挡后序强成员、
  弱符号改绑、双弱合并、common 抽取/合并/吸收、B 类强定义冲突、
  多定义与未解析诊断、归档内多趟、符号分类。
- 裁剪求解器（12）：成组删库、链式依赖保留、主对象钩子、弱改绑强制保库、
  无可改绑弱钩子可删后序库、字典序平局、原清单失败/钩子无定义诊断、多钩子来源。
- `/trim` HTTP 接口（15）：成功裁剪与钩子来源、主对象钩子、库数量越界、
  LTO/非 ELF/非归档/成员重名/重名库/非法钩子名校验、未解析与无定义诊断、
  多钩子混合来源、`/docs` 与 `/health`。
