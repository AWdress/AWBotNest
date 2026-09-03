#!/usr/bin/env node
/**
 * 从根目录 VERSION 文件读取版本号并同步到 package.json
 * 在 npm install 时自动执行
 */
import { readFileSync, writeFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

try {
  // frontend/ 与 VERSION 均属于同一个可独立分发的 AWBotNest 项目。
  const versionFile = join(__dirname, '..', 'VERSION');
  const displayVersion = readFileSync(versionFile, 'utf-8').trim().replace(/^v/i, '');
  // 正式版本直接同步；开发标签才转换为 npm 可接受的预发布版本。
  const version = displayVersion.replace(/_dev$/i, '-dev.0');

  // 读取 package.json
  const packageFile = join(__dirname, 'package.json');
  const packageJson = JSON.parse(readFileSync(packageFile, 'utf-8'));

  // 如果版本号不同，更新并写回
  if (packageJson.version !== version) {
    packageJson.version = version;
    writeFileSync(packageFile, JSON.stringify(packageJson, null, 2) + '\n', 'utf-8');
    console.log(`✓ 已将 package.json 版本号同步为 ${version}`);
  }
} catch (error) {
  console.error('同步版本号失败:', error.message);
  process.exit(1);
}
