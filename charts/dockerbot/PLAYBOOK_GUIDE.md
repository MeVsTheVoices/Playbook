# Dockerbot: quick setup

1. In https://discord.com/developers/applications create an application, then
   open its Bot page. Under Token, select Reset Token and copy the new bot
   token. Keep it private; reset it immediately if it is exposed.
2. On the Bot page enable Message Content Intent for Red's prefix commands.
   Discord may require approval for privileged intents on verified apps.
3. On Installation, enable Guild Install and select the Discord Provided Link.
   Under Guild Install add the bot and applications.commands scopes. Grant only
   the permissions the bot needs (at least View Channels and Send Messages in
   its channels), then open the install link and add it to your server.
4. Import this chart in Playbook, set and save its namespace and values. Create
   the namespace in your cluster first (for example,
   `kubectl create namespace playbook-helm`). If Longhorn is installed, set
   `persistence.storageClassName: longhorn` in the YAML; otherwise the cluster's
   default StorageClass is used. Back up existing bot data before changing
   storage; the full chart README describes migration and PVC lifecycle.
5. Use the Bot token field and confirm Create / update Secret. Use a trusted
   HTTPS connection to Playbook. The Secret name and key come from the saved
   `bot.existingSecret` and `bot.tokenKey` values. Stop any old instance using
   the same token before deploying.
6. Leave Tailscale workload exposure **Off**: Dockerbot connects outbound to
   Discord and this chart does not render a Service. Preview and deploy. Check
   the pod logs for errors, then try `^ping` in a channel the bot can access.
   The default prefix is `^`. If rotating a token, restart the existing
   Deployment so its pod reads the updated Secret.

This image does not include Audio cog support. See the full chart README for
manual installation, migration, backups, and storage options.

Discord's official guide: https://docs.discord.com/developers/quick-start/getting-started
Red's user guide: https://docs.discord.red/en/stable/getting_started.html
