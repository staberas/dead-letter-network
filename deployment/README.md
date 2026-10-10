# Deployment draft

These files are templates. No infrastructure, container registry, hostname,
ingress, donation address, or secrets have been created by this repository.

## Existing host over ZeroTier

Keep the tested native installation and bind its public API to its ZeroTier
IPv4 using `bash run-demo.sh --host YOUR_DLN_ZEROTIER_IP`. The address must be
assigned to the host; check `ip -4 -br addr`. The dashboard defaults to loopback.
Both hosts must be authorized on the ZeroTier network and allowed by its rules.
Check access from the reverse-proxy host before changing the public virtual host:

```sh
curl -sS http://YOUR_DLN_ZEROTIER_IP:8000/healthz
```

Then proxy the public HTTPS hostname to `http://YOUR_DLN_ZEROTIER_IP:8000`,
preserving paths and methods. No public route should target port 8001. With
`--trusted-proxy YOUR_PROXY_ZEROTIER_IP`, the public Uvicorn listener accepts
forwarded client headers only from that exact source address. Configure the
proxy to strip/overwrite spoofable forwarded headers and send the real client
IP. Without this flag the proxy's IP is logged and shares a rate budget.
Local health checks follow the selected bind address. If the host firewall is
enabled, allow TCP 8000 on the ZeroTier interface from the proxy's IPv4 only.
The launcher does not modify firewall, DNS, certificates, or ZeroTier settings.

To bind the dashboard to the private ZeroTier address as well:

```sh
bash run-demo.sh --host YOUR_DLN_ZEROTIER_IP \
  --admin-host YOUR_DLN_ZEROTIER_IP \
  --trusted-proxy YOUR_PROXY_ZEROTIER_IP
```

Browse to `http://YOUR_DLN_ZEROTIER_IP:8001/` over ZeroTier and use the
existing operator credentials in `.demo.env`. Basic authentication remains
required. Allow TCP 8001 from authorized operator devices on the ZeroTier
interface if the host firewall requires it. Do not add port 8001 to the public
reverse proxy. The admin listener always disables proxy-header trust.

## Docker

Use `.env.example` as the configuration reference. Fill a random IP HMAC secret
and independent operator password, each at least 32 random bytes when generated.
`docker compose up --build` binds both listeners to loopback, with a named data
volume and a non-root read-only container. Reverse-proxy **only port 8000** for
public HTTPS. Access 8001 through SSH port forwarding or your private network.
Do not publish the admin listener through the public hostname.

The container uses a supervisor to stop both HTTP processes if either exits.
Only the public application starts the retention loop. Run one replica/worker.

## k3s / Kubernetes

`k8s/dln.yaml` is a starting template for a dedicated `dln` namespace, one
replica, a 2 GiB PVC, restricted security context, and an ingress/egress policy.
Replace the image placeholder with your built image. Create the secret before
starting the pod; no secret is committed:

```sh
kubectl create namespace dln
kubectl -n dln create secret generic dln-secrets \
  --from-literal=DLN_IP_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')" \
  --from-literal=DLN_ADMIN_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
kubectl apply -f deployment/k8s/dln.yaml
kubectl -n dln port-forward deployment/dln 8001:8001
```

The public ClusterIP service exposes only 8000. No admin service or NodePort is
provided. Port forwarding supplies the private operator path. An ingress
controller must run in a namespace you explicitly label `dln-public-edge=true`;
the policy admits only that namespace to 8000. Add your HTTPS ingress separately.
Verify your CNI enforces policies and how it handles host-network ingress and
port forwarding; those details vary. Egress is denied, including cluster APIs.
The service account token is not mounted. Do not assign cluster privileges.

## Edge and data handling

- Bound request bytes to 16 KiB, connections, header sizes, request duration,
  and requests per real client IP at the proxy. Application IPs may be proxy IPs.
- Keep the operator endpoint private, with independent edge throttles if routed.
- Disable access/body/header logging or configure short, documented retention.
  App successful-write IP rows expire after seven days by default; host/proxy
  logs do not inherit that rule.
- Monitor disk use and set storage alerts. Archive time limits are not a global
  storage quota. SQLite returns logical free space after expiry; a 2 GiB PVC
  does not prevent disk exhaustion under sustained abuse.
- Back up with SQLite's backup API, rather than copying the live DB alone while
  WAL is active. Encrypt backups and expire them according to your policy.
- Logical deletion cannot promise physical erasure. If required, design a
  tested secure-delete, FTS cleanup, WAL checkpoint, compaction, and snapshot
  expiry process before accepting sensitive content.
- Choose an operator contact and deployment-specific privacy text before launch.

Pin a reviewed image digest and dependency set for a deployed release. Templates
currently use a floating Python base and bounded dependency ranges for drafting.
