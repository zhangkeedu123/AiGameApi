-- 仅用于开发或测试库。重复导入保留原有玩家资料和进度。
INSERT INTO users (id, nickname, active, created_at)
VALUES ('d4c82460-f5f4-4267-8c96-3e29d08d87ec', '演示玩家', true, statement_timestamp())
ON CONFLICT (id) DO NOTHING;
