# Infra — Symbiote

## Ambientes

| Ambiente | Onde roda | Acesso |
|---|---|---|
| **Local** | Sua máquina de dev | `systemd user` symbios.service — `http://localhost:8003` |
| **Staging** | linux-ci (10.10.10.10) — systemd | `http://10.10.10.10:8008` (sem URL pública) |
| **Produção** | VPS 72.60.246.51 — systemd `symbiote` | `https://symbiote.symlabs.ai` |

## Produção
- **URL pública:** `https://symbiote.symlabs.ai`
- **SSH:** `ssh palhano.services` (alias para 72.60.246.51)
- **Serviço:** `sudo systemctl status symbiote`
- **Porta:** 8008 (nginx faz proxy)
- **Health:** `https://symbiote.symlabs.ai/health`
- **Logs:** `sudo journalctl -u symbiote -f`

## Staging
- **Sem URL pública** — acesso interno apenas
- **Health interno:** `curl http://10.10.10.10:8008/health`
- **Trigger:** push na `main` → Gitea CI
- **Verificar:** `/stage`

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
