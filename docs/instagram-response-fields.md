# Instagram Reels 响应字段与采集规则

## 1. 采集方式

`instagram.py` 使用 Playwright 启动系统 Chrome，加载本地 Cookie 并打开 Instagram 同源页面。采集请求通过网页内 `fetch` 主动发送，不使用点击、展开或滚动操作。通过短码计算作品数字 ID，再核对单作品详情的 `pk` 与 `code`；计算规则已用用户提供的短码与媒体 ID 样本对照。

GraphQL 评论与回复请求在当前登录页面内生成表单参数，使用当前页面可取得的令牌与本地 Cookie。不在文档或代码中写入固定登录 Cookie、CSRF 令牌或其他会话凭据。

本文中的页面监听、展开和滚动说明用于识别抓包样本及数据来源；程序通过对应接口主动请求实现。用于抓包核对时，监听仍须在导航或交互前注册。

## 2. 目标作品匹配

从目标链接路径提取作品短码。例如：

```text
目标链接：https://www.instagram.com/reel/DdO6y3MSSGk/
目标短码：DdO6y3MSSGk
```

当前样本中的视频流响应节点为：

```text
data.xdt_api__v1__clips__home__connection_v2.edges[].node.media
```

遍历所有 `edges`，仅提取 `media.code` 与目标短码完全一致的作品。

- 同一响应可能同时包含目标作品和其他推荐作品。
- 不依赖目标作品在数组中的位置，不固定读取第一条。
- 请求的 Referer、请求名称或 `variables.data.chaining_media_id` 只能用于辅助定位，不能代替响应内容匹配。
- 分页请求即使携带目标短码，也可能只返回后续推荐作品。
- 未捕获匹配响应不代表作品不存在；目标数据也可能通过其他响应结构或首屏 HTML 提供。

匹配示例：

```javascript
const edges =
    json.data?.xdt_api__v1__clips__home__connection_v2?.edges ?? [];

const media = edges
    .map((edge) => edge.node?.media)
    .find((item) => item?.code === targetCode);
```

## 3. 响应分类

| 响应根节点 | 内容 | 处理方式 |
|---|---|---|
| `xdt_api__v1__clips__home__connection_v2` | Reels 视频流 | 遍历作品，按目标短码匹配 |
| `xdt_api__v1__feed__timeline__connection` | 首页时间线 | 不作为本采集路径的目标作品数据源 |
| `xdt_injected_reels_units` | 广告插入内容 | 排除，不记录为目标作品 |

上述节点名称来自当前提供的样本，不保证所有页面或后续版本始终使用相同结构。

## 4. 字段映射

以下字段路径均相对于匹配到的 `media` 对象。

### 4.1 作品基础信息

| 数据 | 字段路径 | 说明 |
|---|---|---|
| 作品短码 | `code` | 与目标链接短码匹配 |
| 媒体数字 ID | `pk` | 保留为字符串，避免大整数精度丢失 |
| 组合 ID | `id` | 当前样本为媒体数字 ID 与作者数字 ID 的组合，保留原值 |
| 发布时间 | `taken_at` | Unix 秒级时间戳 |
| 作品类型 | `product_type` | 当前目标样本为 `clips` |
| 媒体类型 | `media_type` | 当前目标样本为 `2` |
| 文案 | `caption.text` | 作者发布的作品文案，不是用户评论 |
| 文案 ID | `caption.pk` | 保留为字符串 |
| 标签 | 从 `caption.text` 提取 | 当前样本未提供独立标签列表 |

### 4.2 互动数据

| 数据 | 字段路径 | 说明 |
|---|---|---|
| 点赞数 | `like_count` | 记录响应原值 |
| 评论数 | `comment_count` | 不代表已获取评论正文 |
| 再发布数 | `media_repost_count` | 不等同于所有分享行为的总次数 |
| 播放量字段 | `view_count` | 当前目标样本为 `null`，不能据此取得有效播放量 |

### 4.3 作者基础信息

| 数据 | 字段路径 | 说明 |
|---|---|---|
| 作者 ID | `user.pk`、`user.id` | 保留为字符串 |
| 用户名 | `user.username` | 可用于构造作者主页链接 |
| 昵称 | `user.full_name` | 作者显示名称 |
| 头像地址 | `user.profile_pic_url` | 原样记录资源地址 |
| 高清头像地址 | `user.profile_pic_url_hd` | 原样记录资源地址 |
| 认证状态 | `user.is_verified` | 布尔值 |
| 私密账号状态 | `user.is_private` | 布尔值 |

作品详情摘要不足以提供完整作者资料。程序通过 `/api/graphql` 的 `PolarisProfilePageContentQuery` 单独请求，当前文档 ID 为 `28649589871350527`，业务变量以作品作者用户 ID 作为 `id`，并设置 `enable_integrity_filters: true`。

完整作者对象位于 `data.user`。核对返回的 `pk` 或 `id` 与作品作者一致后，将非 `null` 字段合并，保留原摘要有效信息；`0`、`false` 和空简介均作为有效值处理。

| 公开作者资料 | `data.user` 中的字段 |
|---|---|
| 简介 | `biography` |
| 粉丝数 | `follower_count` |
| 关注数 | `following_count` |
| 作品数 | `media_count` |
| 高清头像 | `hd_profile_pic_url_info.url`，补入输出高清头像字段 |
| 外部链接 | `external_url` |
| 账号类别 | `category` |
| 商业账号状态 | `is_business` |
| 专业账号状态 | `is_professional_account` |
| Reels 作品数 | `total_clips_count` |

作者资料请求失败时保留作品、作者摘要和评论，记录 `作者资料采集状态.是否完成: false` 与未完成原因，不将未返回的统计写成零。

### 4.4 视频、封面与音频

| 数据 | 字段路径 | 说明 |
|---|---|---|
| 视频资源列表 | `video_versions[]` | 包含 URL、宽高与类型；条目数量不等于清晰度数量 |
| 视频宽高 | `original_width`、`original_height` | 像素尺寸 |
| 封面候选列表 | `image_versions2.candidates[]` | 包含 URL 和图片尺寸 |
| 首帧图 | `image_versions2.additional_candidates.first_frame.url` | 首帧资源地址 |
| DASH 播放清单 | `video_dash_manifest` | 视频、音频轨道及分段播放信息 |
| 是否包含音频 | `has_audio` | 布尔值 |
| 原创音频信息 | `clips_metadata.original_sound_info` | 包含音频 ID、标题及音频作者信息 |

### 4.5 查看者状态与平台标签

| 数据 | 字段路径 | 说明 |
|---|---|---|
| 当前账号是否点赞 | `has_liked` | 当前登录查看者的状态，不是作品统计 |
| 当前账号是否关注作者 | `user.friendship_status.following` | 当前登录查看者的状态 |
| 是否可再分享 | `can_viewer_reshare` | 响应返回的查看者权限标记 |
| AI 提示标签 | `ai_label_info.subtitle_text` | 平台返回的提示，不视为程序独立鉴定结论 |

## 5. 数据保存规则

- 作品数据必须与目标短码匹配后再保存，避免混入推荐作品或广告。
- ID 按字符串保存，不转换为 JavaScript `Number`。
- 字段缺失或值为 `null` 时保留为空值，不填成 `0`。
- `0`、`false` 与 `null` 分别保存，不使用会混淆这些值的默认值处理方式。
- 文案与评论分别存储，评论数量与评论正文分别存储。
- 再发布数与分享总数分别定义，不混用字段。
- 保存采集时间，统计值仅代表该响应对应的快照。

## 6. 评论数据获取与作品关联

### 6.1 请求与响应识别

评论相关样本使用 `/api/graphql`，因此监听范围不能只包含 `/graphql/query`。

| 请求名称 | 识别用途 |
|---|---|
| `PolarisPostCommentsContainerQuery` | 评论容器请求；结合对应响应中的评论列表节点确认正文数据 |
| `PolarisClipsDesktopCommentsPopoverQuery` | 评论弹层请求；已提供的 `fetch__XDTMediaDict` 响应包含作品标识、评论数和评论状态，不包含评论正文列表 |

评论正文所在节点为：

```text
data.xdt_api__v1__media__media_id__comments__connection.edges[].node
```

先通过作品响应中的 `media.code` 匹配目标，再取得该作品的 `media.pk`。从评论响应对应请求的表单体中解析 `variables.media_id`，仅在其与目标 `media.pk` 相等时关联评论。

评论节点的 `pk` 是评论 ID，`node.user.pk` 是评论者 ID，不能当作作品 ID。当前评论列表响应自身没有提供作品短码，不能只凭请求发生时的页面 URL 或接收顺序判断归属。

### 6.2 评论字段

以下路径相对于评论的 `node`。

| 数据 | 字段路径 | 说明 |
|---|---|---|
| 评论 ID | `pk` | 保留为字符串，用于去重 |
| 评论正文 | `text` | 与作品文案分别存储 |
| 评论时间 | `created_at` | Unix 秒级时间戳 |
| 评论点赞数 | `comment_like_count` | 不是作品点赞数 |
| 子评论数量 | `child_comment_count` | 仅数量，不代表已取得回复正文 |
| 父评论 ID | `parent_comment_id` | `null` 表示样本中未关联父评论 |
| 评论者 ID | `user.pk`、`user.id` | 保留为字符串 |
| 评论者用户名 | `user.username` | 评论发布者，不是作品作者 |
| 评论者头像 | `user.profile_pic_url` | 资源地址 |
| 评论者认证状态 | `user.is_verified` | 布尔值 |
| 是否编辑 | `is_edited` | 保留原值 |
| 是否置顶 | `is_pinned` | 可能为 `null`，不强制转为 `false` |
| 当前查看者是否点赞 | `has_liked_comment` | 当前登录账号的状态 |
| 翻译标记 | `has_translation` | 保留原值 |

`created_at` 是评论发布时间，不是视频发布时间。视频发布时间从作品数据的 `taken_at` 获取；评论响应或作品摘要没有返回 `taken_at` 时，不从评论时间推算视频发布时间。

#### 评论者与回复者主页资料

评论响应的 `user` 为用户摘要。程序在评论及回复收集结束后，按用户 ID 调用 `PolarisProfilePageContentQuery`，复用作者资料字段映射，补充昵称、简介、粉丝数、关注数、作品数、高清头像、主页链接及接口实际提供的其他公开资料。

- 评论者与回复者合并按 ID 去重，每个用户只请求一次；与作品作者相同的用户复用已获取资料。
- 核对返回用户 ID 后才合并，不根据显示名或评论中的提及匹配。
- 保留评论及回复的原始内容、时间戳与关系，不因资料请求失败丢弃评论。
- 单条记录的补充状态输出为 `用户资料采集状态`；总体状态输出在 `采集状态.评论区用户资料采集状态`，记录去重用户数、已补齐和未补齐数量。
- 无用户 ID、资料不可访问或接口未提供的字段不推测填充；HTTP 401、403、429 时停止后续用户资料请求，不循环重试。
- 评论分页完成与用户资料补充完成分别记录；资料未全部补齐时程序退出码为 `2`。

### 6.3 分页与完整性

- 分页信息位于评论列表节点的 `page_info`。
- `has_next_page: true` 表示还有后续页，当前列表不能视为全部评论。
- `end_cursor` 作为不透明分页游标保留，不解析后自行拼接请求。
- 程序通过接口游标加载后续评论，按作品 ID 关联、按评论 ID 去重。
- 回复正文从展开回复的子评论响应提取，按作品 ID 与父评论 ID 关联。
- 作品的 `comment_count`、已获取的顶层评论数和子评论数分别保存，不要求彼此相等。

### 6.4 展开回复

请求路径为 `/api/graphql`，请求名称为 `PolarisPostChildCommentsQuery`。从对应请求的表单体解析 `variables`：

| 参数 | 用途 |
|---|---|
| `media_id` | 回复所属作品的媒体数字 ID，与作品 `pk` 匹配 |
| `parent_comment_id` | 被展开的父评论 ID，与顶层评论 `pk` 匹配 |
| `after`、`before` | 分页参数，首次展开样本中均为 `null` |
| `first`、`last` | 条数参数，首次展开样本中均为 `null` |
| `is_chronological` | 排序参数，当前样本中为 `null`，不能据此断言排序方式 |

回复列表路径为：

```text
data.xdt_api__v1__media__media_id__comments__parent_comment_id__child_comments__connection.edges[].node
```

回复字段与评论字段表一致。回复的 `pk` 为回复自身 ID，`parent_comment_id` 为父评论 ID。保存时同时记录作品 ID、父评论 ID 与回复 ID，并核对响应中的父评论 ID 是否与请求一致。

- 不以 Referer 判断回复归属；页面 URL 可能与实际请求的作品 ID 不一致。
- `user` 是回复发布者；是否为作品作者，通过其 ID 与作品作者 ID 比较。
- 不根据正文中的 `@用户名` 推断直接回复关系，以返回的父评论字段为准。
- 回复分页信息位于该子评论节点的 `page_info`，不能与顶层评论分页状态混用。
- 双向分页参数均存在时，同时保留 `has_next_page`、`has_previous_page` 与对应游标；仅依据当前响应判断是否还有可加载页，不保证永久完整。

### 6.5 顶层评论翻页

请求路径为 `/api/graphql`，请求名称为 `PolarisPostCommentsPaginationQuery`。对应响应仍使用顶层评论列表节点：

```text
data.xdt_api__v1__media__media_id__comments__connection
```

| 请求变量 | 用途 |
|---|---|
| `media_id` | 与目标作品 `pk` 匹配 |
| `after` | 上一页响应 `page_info.end_cursor` 的完整字符串 |
| `before` | 当前样本为 `null` |
| `first` | 请求条数，当前样本为 `10`，不保证响应恰好返回 10 条 |
| `last` | 当前样本为 `null` |
| `sort_order` | 当前样本为 `popular`，不能按时间顺序理解列表 |

本次请求的 `after` 与首屏样本返回的 `end_cursor` 一致。该游标虽然外观为 JSON 字符串，仍应整体保留，不取其中单个字段替代完整游标。

捕获处理规则：

1. 在浏览器内主动请求评论接口。
2. 从响应对应请求的 `variables.media_id` 确认作品归属。
3. 提取 `edges[].node`，按作品 ID 与评论 ID 合并去重。
4. 保存本次 `page_info`；下一页请求的 `after` 使用本次返回的新游标。
5. `has_next_page: false` 表示该响应不再指示后续页；超时、未发出请求或无新增评论不视为已采集完整。

顶层评论分页与每个父评论的回复分页分别记录。回复翻页的非空游标请求尚未提供，不套用顶层评论请求名称或排序参数。

后续样本须保留请求名称、脱敏请求变量及对应响应正文，不保存 Cookie 或会话令牌。不将首屏或单次翻页结果视为完整采集结果。

## 7. 视频播放量获取方式

播放量可从已观察到的单作品详情响应 `items[].play_count`，或作者 Reels 列表中匹配作品后的 `media.play_count` 获取。分别记录来源与采集时间；`view_count`、`play_count`、`ig_play_count` 保留原字段，不因某字段为空而覆盖其他有效统计。播放量不解释为独立观看人数。

### 7.0 按作品 ID 返回详情与播放量

已观察到从作者主页点击作品时触发以下 GET 请求：

```text
/api/v1/media/{media_id}/info/
```

路径中的 `media_id` 是作品数字 ID，不是作者 ID 或链接短码。响应作品列表位于 `items[]`，需核对条目的 `pk` 与目标作品 ID，并用 `code` 辅助核对。

| 数据 | 字段路径 | 说明 |
|---|---|---|
| 作品 ID | `items[].pk` | 与请求路径及目标作品匹配 |
| 作品短码 | `items[].code` | 与目标链接匹配 |
| 播放量 | `items[].play_count` | 当前样本有有效值 |
| Instagram 播放计数字段 | `items[].ig_play_count` | 与 `play_count` 分别保存，不相加，不假定对所有作品都相等 |
| 发布时间 | `items[].taken_at` | Unix 秒级时间戳 |
| 点赞数 | `items[].like_count` | 作品统计 |
| 评论数 | `items[].comment_count` | 不代表已取得评论正文 |
| 视频时长 | `items[].video_duration` | 秒数，保留原始精度或在展示层格式化 |
| 作者信息 | `items[].user` | 按实际返回字段提取 |
| 评论预览 | `items[].preview_comments` | 不作为完整评论列表 |

该响应证明单作品详情路径可携带作品 ID 并返回播放量。`instagram.py` 在当前登录浏览器内主动请求该路径；是否支持其他作品与登录状态需以实际返回为准，不把离线样本解析视为主动调用验证。

程序优先主动请求单作品详情并保存其播放量字段；主页作品列表路径保留为已确认的数据来源，当前脚本不实现主页遍历回退。不要把响应包装层的 `more_available: false` 解读为评论或作者作品列表已经完整。

### 7.1 作者主页采集路径

从目标作品取得作者用户名与作品 ID，进入作者的 `/用户名/reels/` 页面，在作品列表响应中按目标作品 `pk` 或 `code` 精确匹配后读取 `play_count`。同时核对作者 ID，避免混入其他账号的数据。

不能把其他作品的播放量写入目标作品。首批未匹配目标时，通过页面正常滚动继续加载；未找到或停止加载时保持目标播放量未知，不记为零或宣称目标不存在。

主页首批作品请求名称为 `PolarisProfileReelsTabContentQuery`，路径为 `/api/graphql`。业务变量包括：

| 参数 | 用途 |
|---|---|
| `user_id` | 作者用户 ID |
| `data.target_user_id` | 目标作者用户 ID |
| `data.page_size` | 请求列表大小，当前样本为 `12` |
| `data.include_feed_video` | 当前样本为 `true` |

作品列表与播放量字段路径：

```text
data.fetch__XDTUserDict.clips_connection.edges[].node.media
    ├── pk
    ├── code
    ├── user.pk
    ├── play_count
    └── view_count
```

列表分页信息位于 `data.fetch__XDTUserDict.clips_connection.page_info`。主页滚动分页使用下述请求，不套用评论分页逻辑。

#### 作者 Reels 列表分页

请求路径为 `/api/graphql`，请求名称为 `PolarisProfileReelsTabContentQuery_connection`。响应仍位于 `data.fetch__XDTUserDict.clips_connection`。

| 请求变量 | 用途 |
|---|---|
| `id` | 作者用户 ID；首批请求使用的顶层参数名为 `user_id`，不能混淆 |
| `data.target_user_id` | 目标作者用户 ID |
| `after` | 作品列表分页游标，整体保留 |
| `first` | 当前分页样本为 `3` |
| `data.page_size` | 当前分页样本为 `12`；不能仅凭此值推断返回条数 |
| `data.include_feed_video` | 当前样本为 `true` |

每批返回后，核对作者 ID，遍历 `edges[].node.media`，按目标 `pk` 或 `code` 精确匹配，匹配到后保存该条的 `play_count`。多批结果按媒体 ID 去重。

通过页面正常滚动触发后续请求，记录各批请求的 `after` 与响应的 `page_info.end_cursor`。`has_next_page: true` 表示仍可继续加载；为 `false` 时表示该响应不再指示后续页，未找到目标仍保存为未知。中途停止、超时或没有新增作品不作为列表已加载完整的证据。

随机截取的中间页请求可以确认分页字段与响应结构，但没有相邻上一页时，不能声称已验证该请求游标与上一页返回游标连续。

### 7.2 路由导航响应与作品数据的区别

`/ajax/navigation/` 请求中的 `route_url` 指定导航目标。当前提供的响应为 `route_definition`，用于描述页面路由，不是作品播放量列表。

响应正文包含 `for (;;);` 前缀。分析该类文本时，仅移除前缀后解析 JSON，不执行响应文本。该响应也含会话令牌字段，不整体写入日志或采集结果。

Playwright 在进入主页前监听实际作品列表响应；站内点击导航与直接 `goto` 不保证产生同一组请求，不依赖必定出现 `/ajax/navigation/`。

### 7.3 排除非作品数据请求

| 请求名称 | 当前样本响应节点 | 数据类型 | 是否提供目标作品播放量 |
|---|---|---|---|
| `PolarisProfileSuggestedUsersWithPreloadableQuery` | `xdt_api__v1__discover__chaining.users` | 推荐账号列表 | 否 |
| `QuickPromotionSupportIGSchemaBatchFetchQuery` | `ig_quick_promotion_batch_fetch_root` | 界面推广或引导配置 | 否 |

推荐账号请求中的 `target_id` 是推荐所依据的用户 ID，不是作品 ID。推荐列表中的用户也不能作为目标作品作者资料保存。界面配置响应中的空 `eligible_promotions.edges` 仅表示该项没有返回可用推广内容，不表示作者没有作品或播放量为零。

## 附录 A：样本事实快照

核对时间：2026-10-20（Asia/Shanghai）。依据用户提供的响应文件离线解析，未进行实时平台查询。

目标链接：`https://www.instagram.com/reel/DdO6y3MSSGk/`。

```json
{
    "code": "DdO6y3MSSGk",
    "pk": "3985381302202868132",
    "id": "3985381302202868132_11792660243",
    "taken_at": 1789314558,
    "product_type": "clips",
    "media_type": 2,
    "author": {
        "id": "11792660243",
        "username": "quangphamvs660",
        "full_name": "Lydia",
        "is_verified": false,
        "is_private": false
    },
    "like_count": 154,
    "comment_count": 0,
    "media_repost_count": 14,
    "view_count": null,
    "original_width": 720,
    "original_height": 1280,
    "has_audio": true
}
```

该样本共返回 7 条作品，目标作品位于第一条。此位置仅为样本事实，不作为采集规则。

样本来源：用户附件 `1e051ab1-c27b-452a-8c76-4d072e41cda0/已粘贴的文本.txt`。

## 附录 B：评论样本事实快照

核对时间：2026-10-20（Asia/Shanghai）。依据用户提供的评论附件与请求、响应文本离线核对，未执行 cURL 或实时平台查询。

本次评论示例作品为 `Dd-3mC7IBXM`，不等于目标列表中的第二条 `DdVINXQhTF_`，不据此改写目标列表。

用户提供的两条评论相关请求均包含以下变量：

```json
{
    "media_id": "3998878026189247948"
}
```

用户提供的 `fetch__XDTMediaDict` 响应将该媒体 ID 与短码 `Dd-3mC7IBXM` 关联，并返回 `comment_count: 1977`。

评论附件包含 15 条评论，均为 `parent_comment_id: null`，`page_info.has_next_page` 为 `true`。第一条评论摘要如下：

```json
{
    "pk": "18114126323326624",
    "text": "Price",
    "created_at": 1791006576,
    "comment_like_count": 5,
    "child_comment_count": 7,
    "parent_comment_id": null,
    "user": {
        "pk": "70432826350",
        "username": "food_otsu",
        "is_verified": false
    }
}
```

评论附件本身不携带作品 ID 或短码；其与请求的配对以用户提供的抓包上下文为依据，程序运行时必须读取对应请求的 `variables.media_id` 确认归属。

样本来源：用户附件 `869aebf4-36de-46e1-996e-247fb97cc797/已粘贴的文本.txt` 及本次提供的请求、响应文本。未保存请求中的会话凭据。

## 附录 C：展开回复样本事实快照

核对时间：2026-10-20（Asia/Shanghai）。依据用户提供的请求文本与回复附件离线解析，未执行请求。

首次展开请求的业务变量：

```json
{
    "after": null,
    "before": null,
    "media_id": "3998878026189247948",
    "parent_comment_id": "18114126323326624",
    "is_chronological": null,
    "first": null,
    "last": null
}
```

该父评论为附录 B 第一条评论，正文为 `Price`，此前返回 `child_comment_count: 7`。本次回复列表返回 7 条，所有条目的 `parent_comment_id` 均与请求一致。

回复分页状态：

```json
{
    "end_cursor": null,
    "has_next_page": false,
    "has_previous_page": false,
    "start_cursor": null
}
```

第一条回复的 ID 为 `18138014743626033`，发布者为 `josenmachinery`，发布者 ID 为 `61923782809`，评论点赞数为 `6`，`created_at` 为 `1791009783`。该发布者 ID 与附录 B 中的作品作者 ID 一致。

请求 Referer 的短码为 `DdO6y3MSSGk`，但 `variables.media_id` 为 `3998878026189247948`，根据附录 B 对应作品为 `Dd-3mC7IBXM`。本次回复按请求中的作品 ID 归属，不按 Referer 归属。

样本来源：用户附件 `94f246a7-8fdc-42fd-a12a-9f6c78225093/已粘贴的文本.txt` 及本次请求文本。未保存会话凭据。

## 附录 D：顶层评论翻页样本事实快照

核对时间：2026-10-20（Asia/Shanghai）。依据用户提供的翻页请求文本和附件离线解析，未执行请求。

- 请求名称：`PolarisPostCommentsPaginationQuery`。
- 请求中的 `media_id`：`3998878026189247948`，对应附录 B 的作品 `Dd-3mC7IBXM`。
- 请求中的 `after`：与附录 B 首屏响应的完整 `end_cursor` 一致。
- 请求中的 `first`：`10`；`sort_order`：`popular`。
- 本次响应实际返回 15 条，均为 `parent_comment_id: null`。
- 与附录 B 首屏的 15 条按评论 `pk` 比较，无重复，合计 30 条不同的顶层评论。
- 本次返回新的 `end_cursor`，与首屏游标不同；`has_next_page: true`、`has_previous_page: false`，说明仍有后续页。
- 第一条评论 ID：`17914065468533606`，正文为 `price`，评论者为 `sumant38112`，`child_comment_count: 1`。
- 请求 Referer 仍为 `DdO6y3MSSGk`，不用于覆盖请求中作品 ID 的关联结果。

样本来源：用户附件 `0992b7d3-4a80-4125-83d2-192c029d77fd/已粘贴的文本.txt` 及本次请求文本。未保存会话凭据。

## 附录 E：作者主页导航样本事实快照

核对时间：2026-10-20（Asia/Shanghai）。依据用户提供的导航请求与附件离线解析，未执行请求或实时查询播放量。

- 请求路径：`/ajax/navigation/`。
- 请求中的 `route_url`：`/quangphamvs660/reels/`。
- 响应的 `payload.payload.result.type`：`route_definition`。
- 响应路由参数中的用户名：`quangphamvs660`，与附录 A 目标作品作者一致。
- 解析后的响应中未发现 `play_count` 或 `view_count` 字段，也未发现目标短码 `DdO6y3MSSGk` 或媒体 ID `3985381302202868132`。
- 该样本证明导航目标为作者 Reels 页，尚不能据此取得或验证目标作品播放量。

样本来源：用户附件 `0eb7efa3-9186-47ad-9bb9-87a33e404f44/已粘贴的文本.txt` 及本次请求文本。未保存会话凭据或响应中的令牌值。

## 附录 F：主页辅助请求样本事实快照

核对时间：2026-10-20（Asia/Shanghai）。依据用户提供的附件和请求、响应文本离线解析，未执行请求。

- 推荐账号请求变量为 `module: profile`、`target_id: 11792660243`；该 ID 与附录 A 的作品作者 ID 一致。
- 推荐账号附件返回 30 个用户，包含用户名、昵称、用户 ID、头像、认证和私密状态等；没有 `play_count`、`view_count`、`code`、`shortcode` 或 `media_id` 字段。
- 两条界面配置请求返回的 `eligible_promotions.edges` 均为空，没有作品列表或播放量。
- 本组样本不改变播放量来源待验证的状态。

样本来源：用户附件 `64d11d54-f997-40be-a376-cebf876f0cc6/已粘贴的文本.txt` 及本次请求、响应文本。未保存会话凭据。

## 附录 G：作者 Reels 列表播放量样本事实快照

核对时间：2026-10-20（Asia/Shanghai）。依据用户提供的请求和附件离线解析，未执行请求或实时查询。

- 请求名称：`PolarisProfileReelsTabContentQuery`。
- `user_id` 与 `data.target_user_id` 均为 `11792660243`，作者为 `quangphamvs660`。
- 请求的 `data.page_size` 为 `12`，响应实际返回 12 条作品。
- 每条作品包含数字型 `play_count`，本批 `view_count` 均为 `null`。
- 示例：`DepAb6TJcQm` 的 `pk` 为 `4010738861174277158`，`play_count: 232`；`DepAbGmJnhw` 的 `play_count: 6083`。
- 本批未包含目标短码 `DdO6y3MSSGk` 或目标媒体 ID `3985381302202868132`，不能据此给该目标填写播放量。
- `page_info.has_next_page: true`，并返回非空 `end_cursor`，需要继续加载主页作品列表才能查找后续作品。
- 已确认主页列表中存在播放量字段；目标作品的具体播放量及主页后续分页请求仍未取得。

样本来源：用户附件 `cea0c5a3-c59c-48d7-9acd-392ad5b1ac33/已粘贴的文本.txt` 及本次请求文本。未保存会话凭据。

## 附录 H：作者 Reels 列表分页样本事实快照

核对时间：2026-10-20（Asia/Shanghai）。依据用户提供的请求与附件离线解析，未执行请求。

- 用户说明该样本为多次滚动后随机截取的一页，不认定为附录 G 首批结果的紧邻下一页。
- 请求名称：`PolarisProfileReelsTabContentQuery_connection`。
- 请求中的 `id`、`data.target_user_id` 与响应 `fetch__XDTUserDict.id` 均为 `11792660243`。
- 请求包含非空 `after`、`first: 3`、`data.page_size: 12`。
- 响应实际返回 3 条作品：

| 作品短码 | 媒体 ID | `play_count` |
|---|---|---:|
| `DeHgepdJhEu` | `4001309637527081262` | 5444 |
| `DeHgf8nSdN3` | `4001309726817768311` | 5202 |
| `DeHgcwcSts0` | `4001309507589954356` | 1950 |

- 3 条作品的 `view_count` 均为 `null`，作者 ID 均为 `11792660243`。
- 目标作品 `DdO6y3MSSGk` 不在本批中，具体播放量仍未取得。
- 响应返回非空 `end_cursor` 与 `has_next_page: true`。
- 本次请求的 `after` 与附录 G 首批返回的 `end_cursor` 不同，未提供相邻上一页，未核验中间各页游标连续性。

样本来源：用户附件 `25066d40-609b-4f2d-9b61-00e38ef31b53/已粘贴的文本.txt` 及本次请求文本。未保存会话凭据。

## 附录 I：单作品详情播放量样本事实快照

核对时间：2026-10-20（Asia/Shanghai）。依据用户提供的请求与附件离线解析，未重放请求。

- 用户观察到的触发操作：从作者主页点击一条作品。
- GET 请求路径：`/api/v1/media/4007653782021663135/info/`。
- 响应 `num_results: 1`，返回一条作品。
- 作品 `pk`：`4007653782021663135`；短码：`DeeC-GcJumf`。
- 作者：`quangphamvs660`，作者 ID：`11792660243`。
- `play_count: 716`、`ig_play_count: 716`；当前条目没有 `view_count` 字段。
- `like_count: 2`、`comment_count: 0`、`taken_at: 1791969660`、`video_duration: 6.034999847412109`。
- 该作品不是原目标 `DdO6y3MSSGk`，不能据此给原目标填写播放量。

样本来源：用户附件 `8b9e6ba9-6752-41fb-82f2-26e5293181ab/已粘贴的文本.txt` 及本次请求文本。未保存会话凭据。

## 附录 J：Python 程序实测快照

核对时间：2026-10-20（Asia/Shanghai）。使用本地已保存 Cookie、系统 Chrome 无头模式及网页内主动 `fetch`，没有点击或滚动操作。

- `DdO6y3MSSGk`：单作品详情主动请求成功，`play_count: 138942`，`comment_count: 0`，已保存 `data/DdO6y3MSSGk.json`。
- `Dd-3mC7IBXM`：单作品详情主动请求成功，`play_count: 4504716`，`comment_count: 1977`。
- 对 `Dd-3mC7IBXM` 使用 `--max-pages 1` 进行有限评论验证，取得 15 条顶层评论及其 32 条回复。由于主动限制顶层分页，仅保存部分结果，`comments_complete: false`，退出码为 `2`，不视为完整评论采集。
- 单独验证顶层评论翻页：首批 15 条，以其返回游标主动请求下一页，又取得 15 条；下一页仍返回 `has_next_page: true`，未继续全量采集。
- 本地 HTTP 组合样本测试使用真实 Chrome 网页 `fetch`，覆盖详情、评论分页与回复的完整执行路径；该测试不证明真实平台全量评论已取完。
