# Infra — Symbiote

## Ambientes

| Ambiente | Onde roda | Acesso |
|---|---|---|
| **Local** | Sua máquina de dev | `systemd user` symbios.service — `http://localhost:8003` |
| **Staging** | linux-ci (10.10.10.10) — Docker (`staging-symbiote`) | `http://127.0.0.1:8008` no host, via SSH |
| **Produção** | VPS 72.60.246.51 — systemd `symbiote` | `https://symbiote.symlabs.ai` |

## Produção
- **URL pública:** `https://symbiote.symlabs.ai`
- **SSH:** `ssh palhano.services` (alias para 72.60.246.51)
- **Serviço:** `sudo systemctl status symbiote`
- **Porta:** 8008 (nginx faz proxy)
- **Health:** `https://symbiote.symlabs.ai/health`
- **Logs:** `sudo journalctl -u symbiote -f`

## Staging
- **Container:** `staging-symbiote` no docker-compose do linux-ci (`~/staging/docker-compose.yml`)
- **Porta:** `127.0.0.1:8008` no host; não exposta na LAN
- **Sem URL pública** — acesso por túnel SSH quando necessário
- **Health interno:** `ssh linux-ci "curl -fsS http://127.0.0.1:8008/health"`
- **Trigger:** push na `main` → testes → build da imagem → deploy do commit exato
- **Runtime:** imagem imutável, sem bind mount de `/app/src` e sem `--reload`
- **Dependências:** CI e imagem usam locks Linux/Python 3.12 em `requirements/`; nenhum build resolve versões novas implicitamente
- **Rollback:** a imagem e o commit anteriores são restaurados automaticamente se health, versão ou contrato do container falharem
- **Backup:** SQLite usa backup online consistente antes do recreate; cópia externa ao volume fica em `~/staging/backups/symbiote/`
- **Restore:** restauração do banco é manual; o deploy não executa downgrade destrutivo de schema
- **Logs:** `ssh linux-ci "cd ~/staging && docker compose logs -f symbiote"`
- **Restart manual:** `ssh linux-ci "cd ~/staging && docker compose restart symbiote"`

## Deploy
```bash
/deploy   # executa promote — valida staging, deploya, verifica versão
```

## Credenciais
**Nunca peça credenciais ao usuário.** Estão no SymVault:
- Projeto vault: `symbiote`
- Para acessar: `/ask devops`

## Dúvidas de infra
Porta, nginx, SSL, secrets, rollback → `/ask devops`
