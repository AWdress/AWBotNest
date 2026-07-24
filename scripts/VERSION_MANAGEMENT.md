# 版本号统一管理

## 设计原理

**单一真值源**：所有版本号从根目录 `VERSION` 文件读取，避免多处手动维护导致不一致。

## 版本号位置

| 文件 | 读取方式 |
|------|---------|
| `VERSION` | **唯一真值源**，手动编辑或用脚本更新 |
| `webui/api.py` | 启动时读取 `VERSION` 文件 |
| `webui/frontend/package.json` | `npm install` 时自动从 `VERSION` 同步 |
| `CHANGELOG.md` | 手动维护（脚本可添加占位符） |
| `docs/API.md` | 手动维护（脚本可添加占位符） |

## 发布新版本流程

### 方法一：使用自动化脚本（推荐）

```bash
# 1. 运行版本号更新脚本
python scripts/update_version.py 1.2.3.4

# 2. 编辑 CHANGELOG.md 和 docs/API.md，补充更新说明

# 3. 提交并打标签
git add -A
git commit -m "发布 v1.2.3.4：更新说明"
git push origin main
git tag v1.2.3.4
git push origin v1.2.3.4 --force
```

### 方法二：手动更新

```bash
# 1. 修改 VERSION 文件
echo "1.2.3.4" > VERSION

# 2. 在 CHANGELOG.md 开头添加新版本说明
# 3. 在 docs/API.md 更新日志中添加新版本
# 4. 同步前端版本号
cd webui/frontend
node sync-version.js
# 或者
npm install

# 5. 提交并打标签（同上）
```

## 验证版本号一致性

```bash
# 检查所有版本号
grep -rn "1\.1\.2\." --include="*.py" --include="*.json" --include="*.md" | grep -v node_modules | grep -v .venv
```

## 注意事项

- `package-lock.json` 会在 `npm install` 时自动更新，无需手动修改
- Python 后端启动时动态读取 `VERSION`，无需重新构建
- 前端需要重新 `npm install` 才能同步版本号
- Docker 镜像构建时会包含 `VERSION` 文件
