# bernardpoignant.fr

Petit CMS Flask pour publier des articles sur **bernardpoignant.fr**. Front public minimaliste, back-office privé avec éditeur WYSIWYG (Quill) et stockage HTML.

Modelé d'après les apps `seo/` et `fhollande2026/` du même cluster — même registre OVH, même ingress nginx + cert-manager, même workflow GitHub Actions.

## Pile

- **Backend** : Flask 3, SQLAlchemy, Flask-Login, gunicorn
- **WYSIWYG** : [Quill 2](https://quilljs.com) (CDN, sans clé d'API)
- **Sécurité** : sortie HTML nettoyée par `bleach` à l'enregistrement
- **DB** : SQLite (1 fichier, monté via PVC en prod). Pas de MySQL à provisionner.
- **Auth** : nom d'utilisateur / mot de passe (hash Werkzeug), seed du premier admin au démarrage

## Lancer en local

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env       # ajuster ADMIN_PASSWORD / SECRET_KEY si besoin
python run.py --debug
```

Puis ouvrir **http://127.0.0.1:5007**.

- Site public : `/`
- Espace admin : `/admin/login` (par défaut : `admin` / `38HytheRoad$`)

Le fichier SQLite est créé dans `instance/bpoignant.db` au premier lancement.

## Comptes administrateur

Au tout premier démarrage, si la table `users` est vide, un compte admin est créé automatiquement à partir des variables d'environnement :

| Variable          | Valeur par défaut |
|-------------------|-------------------|
| `ADMIN_USERNAME`  | `admin`           |
| `ADMIN_PASSWORD`  | `38HytheRoad$`    |

Une fois connecté, vous pouvez créer d'autres utilisateurs depuis **Admin → Utilisateurs**.

⚠️ Si vous changez `ADMIN_PASSWORD` après le premier démarrage, **rien ne se passe** : le mot de passe d'un compte existant ne se modifie qu'via l'UI ou en repartant d'une base vierge.

## Modèle de données

```
users
├── id, username (unique), password_hash, is_admin, created_at, last_login

articles
├── id, title, slug (unique), summary, content_html
├── published (bool), published_at, created_at, updated_at
└── author_id → users.id
```

`content_html` est le HTML produit par Quill, passé dans `bleach` avec une liste blanche de tags / attributs (titres, listes, liens, images, citations, code, tableaux). Tout ce qui sort de la liste est strippé à l'enregistrement.

## Déploiement

Le push sur `main` déclenche `.github/workflows/deploy.yml` qui :

1. Construit l'image Docker et la pousse vers OVH : `bpoignant/bpoignant:latest` (+ `:<sha>`)
2. Recrée le secret K8s `bpoignant-secrets` à partir du secret GitHub `BPOIGNANT_SECRETS_JSON`
3. Applique `kubernetes/deployment.yaml` (PVC + Deployment + Service + Ingress pour `bernardpoignant.fr` et `www.bernardpoignant.fr`)
4. Restart + status du rollout

### Secrets GitHub requis

Réutiliser ceux du repo `seo` / `fhollande2026` :

- `OVH_REGISTRY_USER`
- `OVH_REGISTRY_PASSWORD`
- `KUBECONFIG_B64` — kubeconfig encodé en base64
- `BPOIGNANT_SECRETS_JSON` — JSON dont chaque clé devient une variable d'env dans le pod :

```json
{
  "SECRET_KEY": "une-longue-chaine-aleatoire",
  "ADMIN_USERNAME": "admin",
  "ADMIN_PASSWORD": "38HytheRoad$"
}
```

`DATABASE_URL` et `CACHE_TYPE` sont déjà fixés en dur dans `deployment.yaml`.

### Pré-requis cluster (déjà en place)

- `ovh-registry-secret` (imagePullSecret) — partagé avec les autres apps
- ClusterIssuer `letsencrypt-prod` + ingress controller nginx
- StorageClass par défaut (le PVC `bpoignant-data` réclame 1 Gi en `ReadWriteOnce`)

### DNS

Faire pointer `bernardpoignant.fr` (et `www.bernardpoignant.fr`) vers l'IP du LoadBalancer nginx (la même que `seo.lesbonneschoses.app` / `fhollande.fr`).

## Arborescence

```
.
├── __init__.py                  # factory Flask
├── wsgi.py                      # entrypoint gunicorn
├── run.py                       # dev local
├── init_db.py                   # instance SQLAlchemy
├── init_cache.py                # instance Flask-Caching
├── auth/                        # users + login (username/password)
│   ├── __init__.py
│   ├── models.py
│   └── templates/{login,users,user_form}.html
├── articles/                    # CMS — public + admin CRUD
│   ├── __init__.py              # 2 blueprints : articles_bp (public) + admin_articles_bp
│   ├── models.py
│   └── templates/articles_{public_list,public_show,admin_list,admin_form}.html
├── templates/{base,404}.html
├── static/css/style.css
├── Dockerfile
├── kubernetes/deployment.yaml   # PVC + Deployment + Service + Ingress
└── .github/workflows/deploy.yml
```

## Sauvegarde de la base

Le SQLite vit dans le PVC `bpoignant-data` (montage `/data`). Pour exporter :

```bash
kubectl exec deploy/bpoignant -- cat /data/bpoignant.db > backup-$(date +%F).db
```

Pour restaurer, `kubectl cp` un fichier dans le pod puis redémarrer le déploiement.
