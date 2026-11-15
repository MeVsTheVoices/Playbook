# Dockerbot

The recovered Compose configuration is retained for rollback. To run this bot
on Kubernetes instead, see the [Dockerbot Helm chart](../../charts/dockerbot/),
including its Longhorn and data migration instructions. Stop the Compose bot
before starting the chart; do not run both instances with the same token.
