#!/usr/bin/env bash
# Разовая инициализация OpenBao: init (1 unseal key — single-node деплой),
# unseal, включение KV v2, политика read-only для приложения, токен приложения,
# запись секретов. Ключи/root-токен -> secrets/openbao-keys.json (chmod 600,
# в git не попадает). Токен приложения -> .env.openbao (env_file compose).
#
# Использование:
#   scripts/openbao_bootstrap.sh path/to/seed.json
#
# seed.json — {"telegram_bot_token": "...", "telegram_channel_id": "...",
#              "telegram_admin_ids": "...", "github_token": "...",
#              "groq_api_key": "..."}
set -euo pipefail

ADDR="http://127.0.0.1:8200"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="$REPO_ROOT/secrets"
KEYS_FILE="$SECRETS_DIR/openbao-keys.json"
ENV_OPENBAO="$REPO_ROOT/.env.openbao"
SEED_FILE="${1:?использование: $0 path/to/seed.json}"

mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

echo "==> Жду OpenBao на $ADDR..."
for i in $(seq 1 30); do
  if curl -sf "$ADDR/v1/sys/health?standbyok=true&uninitcode=200&sealedcode=200" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

STATUS=$(curl -s "$ADDR/v1/sys/health?standbyok=true&uninitcode=200&sealedcode=200")
INITIALIZED=$(echo "$STATUS" | uv run python -c "import json,sys; print(json.load(sys.stdin).get('initialized', False))")

if [ "$INITIALIZED" != "True" ]; then
  echo "==> Инициализирую (1 unseal key, single-node)..."
  curl -s --request POST "$ADDR/v1/sys/init" \
    --data '{"secret_shares":1,"secret_threshold":1}' > "$KEYS_FILE"
  chmod 600 "$KEYS_FILE"
  echo "==> Ключи сохранены в $KEYS_FILE — НЕ теряй этот файл, он не в git."
else
  echo "==> Уже инициализирован, использую существующий $KEYS_FILE"
fi

UNSEAL_KEY=$(uv run python -c "import json; print(json.load(open('$KEYS_FILE'))['keys'][0])")
ROOT_TOKEN=$(uv run python -c "import json; print(json.load(open('$KEYS_FILE'))['root_token'])")

SEALED=$(curl -s "$ADDR/v1/sys/health?standbyok=true&uninitcode=200&sealedcode=200" | uv run python -c "import json,sys; print(json.load(sys.stdin).get('sealed', True))")
if [ "$SEALED" = "True" ]; then
  echo "==> Unseal..."
  curl -s --request POST "$ADDR/v1/sys/unseal" --data "{\"key\":\"$UNSEAL_KEY\"}" >/dev/null
fi

echo "==> Включаю KV v2 на secret/ (если ещё не включён)..."
curl -s --header "X-Vault-Token: $ROOT_TOKEN" "$ADDR/v1/sys/mounts/secret" \
  --request POST --data '{"type":"kv-v2"}' >/dev/null || true

echo "==> Пишу read-only политику ossdigest-read..."
POLICY_FILE=$(mktemp)
cat > "$POLICY_FILE" <<'HCL'
path "secret/data/ossdigest" { capabilities = ["read"] }
HCL
curl -s --header "X-Vault-Token: $ROOT_TOKEN" "$ADDR/v1/sys/policies/acl/ossdigest-read" \
  --request PUT --data "$(uv run python -c "
import json
print(json.dumps({'policy': open('$POLICY_FILE').read()}))
")" >/dev/null
rm -f "$POLICY_FILE"

echo "==> Создаю токен приложения (ttl 8760h, привязан к ossdigest-read)..."
APP_TOKEN=$(curl -s --header "X-Vault-Token: $ROOT_TOKEN" "$ADDR/v1/auth/token/create" \
  --request POST --data '{"policies":["ossdigest-read"],"ttl":"8760h","renewable":true}' \
  | uv run python -c "import json,sys; print(json.load(sys.stdin)['auth']['client_token'])")

echo "==> Пишу секреты из $SEED_FILE в secret/data/ossdigest..."
uv run python -c "
import json
seed = json.load(open('$SEED_FILE'))
print(json.dumps({'data': seed}))
" > /tmp/ossdigest-seed-payload.json
curl -s --header "X-Vault-Token: $ROOT_TOKEN" "$ADDR/v1/secret/data/ossdigest" \
  --request POST --data @/tmp/ossdigest-seed-payload.json >/dev/null
rm -f /tmp/ossdigest-seed-payload.json

cat > "$ENV_OPENBAO" <<EOF
OPENBAO_TOKEN=$APP_TOKEN
EOF
chmod 600 "$ENV_OPENBAO"

echo "==> Готово. .env.openbao записан (OPENBAO_ADDR укажи в .env как http://openbao:8200)."
echo "==> Проверка чтения:"
curl -s --header "X-Vault-Token: $APP_TOKEN" "$ADDR/v1/secret/data/ossdigest" \
  | uv run python -c "import json,sys; d=json.load(sys.stdin)['data']['data']; print('ключи в секрете:', list(d.keys()))"
