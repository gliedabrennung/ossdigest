storage "file" {
  path = "/openbao/file"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = "true"
}

# TLS отключён т.к. порт открыт только на loopback хоста и во внутренней
# docker-сети (см. docker-compose.yml) — трафик не покидает хост.
disable_mlock = true
ui            = false
