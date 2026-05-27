"""Initial HTML content for the three baseline pages. Only used on first
boot to populate the `pages` table — after that the admin edits via the
WYSIWYG editor. Re-running the seed never overwrites existing rows.
"""


BIOGRAPHIE_HTML = """
<p>
    <strong>Bernard Poignant</strong> est né le 19 septembre 1945 à Vannes.
    Agrégé d'histoire, il a longtemps enseigné avant de se consacrer pleinement à la vie politique.
</p>

<h2>Un élu breton du Parti socialiste</h2>
<p>
    Membre du Parti socialiste depuis les années 1970, il est élu pour la première fois
    <strong>député du Finistère</strong> en 1981, mandat qu'il exerce jusqu'en 1986 puis de 1988 à 1993.
    Il devient ensuite <strong>maire de Quimper</strong> à deux reprises&nbsp;: de 1989 à 2001, puis de 2008 à 2014.
    Il a également siégé au conseil régional de Bretagne et au conseil général du Finistère.
</p>

<h2>Député européen</h2>
<p>
    Élu au <strong>Parlement européen</strong> de 1999 à 2009, il y préside la délégation socialiste française
    et s'investit particulièrement sur les questions maritimes, la sécurité en mer et la réforme
    de la politique commune de la pêche.
</p>

<h2>Conseiller de François Hollande</h2>
<p>
    Proche de François Hollande de longue date, il devient l'un de ses <strong>conseillers à l'Élysée</strong>
    pendant le quinquennat 2012-2017. En 2017, il choisit de soutenir la candidature d'Emmanuel Macron.
</p>

<h2>Distinction</h2>
<p>
    Bernard Poignant a été promu <strong>officier de la Légion d'honneur</strong> en 2022, en reconnaissance
    de plusieurs décennies d'engagement au service de la chose publique.
</p>

<p style="color: var(--gris); font-size: 0.9rem; margin-top: 2rem;">
    Source&nbsp;: <a href="https://fr.wikipedia.org/wiki/Bernard_Poignant" target="_blank" rel="noopener">Wikipédia</a>.
</p>
""".strip()


MENTIONS_LEGALES_HTML = """
<h2>Éditeur du site</h2>
<p>Le site <strong>bernardpoignant.fr</strong> est édité à titre personnel par&nbsp;:</p>
<ul>
    <li><strong>Directeur de la publication&nbsp;:</strong> Bernard Poignant</li>
    <li><strong>Contact&nbsp;:</strong> <a href="mailto:contact@bernardpoignant.fr">contact@bernardpoignant.fr</a></li>
</ul>

<h2>Hébergement</h2>
<p>Le site est hébergé par&nbsp;:</p>
<ul>
    <li><strong>OVH SAS</strong></li>
    <li>2 rue Kellermann, 59100 Roubaix, France</li>
    <li>Téléphone&nbsp;: +33 9 72 10 10 07</li>
</ul>

<h2>Propriété intellectuelle</h2>
<p>
    L'ensemble des contenus publiés sur ce site (textes, images, mise en page) est protégé
    par le droit d'auteur. Toute reproduction, représentation, modification ou exploitation,
    par quelque procédé que ce soit, est interdite sans l'autorisation préalable et écrite de
    l'éditeur, sauf dans le cadre des courtes citations prévues par l'article L.122-5 du
    Code de la propriété intellectuelle, à condition d'indiquer clairement la source.
</p>

<h2>Responsabilité</h2>
<p>
    L'éditeur s'efforce d'assurer l'exactitude et la mise à jour des informations diffusées
    sur ce site. Il ne peut toutefois être tenu responsable d'erreurs, d'omissions ou de
    l'indisponibilité des informations. Les liens vers des sites externes sont fournis à
    titre informatif et l'éditeur n'a aucun contrôle sur leur contenu.
</p>

<h2>Données personnelles</h2>
<p>
    Le traitement des données personnelles (formulaire de newsletter, mesure d'audience) est
    détaillé dans la <a href="/confidentialite">politique de confidentialité</a>.
</p>

<h2>Contact</h2>
<p>
    Pour toute question relative au site&nbsp;:
    <a href="mailto:contact@bernardpoignant.fr">contact@bernardpoignant.fr</a>.
</p>
""".strip()


CONFIDENTIALITE_HTML = """
<p style="color: var(--gris); font-style: italic;">Conforme au RGPD et à la loi Informatique et Libertés.</p>

<h2>Responsable du traitement</h2>
<p>
    Le responsable du traitement des données collectées sur bernardpoignant.fr est
    <strong>Bernard Poignant</strong>. Pour toute question relative à vos données&nbsp;:
    <a href="mailto:contact@bernardpoignant.fr">contact@bernardpoignant.fr</a>.
</p>

<h2>Données collectées et finalités</h2>

<h3>1. Inscription à la newsletter</h3>
<p>Lorsque vous vous inscrivez à la newsletter, nous collectons&nbsp;:</p>
<ul>
    <li>votre <strong>adresse e-mail</strong> (obligatoire)&nbsp;;</li>
    <li>vos <strong>prénom, nom et ville</strong> (facultatifs)&nbsp;;</li>
    <li>la <strong>date de l'inscription</strong>.</li>
</ul>
<p>
    <strong>Finalité&nbsp;:</strong> vous informer de la publication de nouveaux articles.<br>
    <strong>Base légale&nbsp;:</strong> votre consentement (article 6.1.a du RGPD).<br>
    <strong>Durée de conservation&nbsp;:</strong> jusqu'à votre désinscription. Chaque e-mail
    envoyé contient un lien de désinscription en un clic.
</p>

<h3>2. Mesure d'audience</h3>
<p>Pour comprendre l'usage du site (pages consultées, sources de trafic), nous enregistrons à chaque visite&nbsp;:</p>
<ul>
    <li>la <strong>page consultée</strong> et la <strong>page de provenance</strong>&nbsp;;</li>
    <li>un <strong>identifiant pseudonymisé du visiteur</strong> (empreinte SHA-256 calculée
        à partir de l'adresse IP et du navigateur, recombinée avec un sel quotidien)&nbsp;;</li>
    <li>la <strong>chaîne user-agent</strong> du navigateur&nbsp;;</li>
    <li>le <strong>code pays</strong> (déduit par l'hébergeur, sans précision géographique fine).</li>
</ul>
<p>
    L'adresse IP brute n'est <strong>jamais stockée</strong>. L'identifiant change chaque
    jour, ce qui empêche tout suivi entre journées différentes.
</p>
<p>
    <strong>Finalité&nbsp;:</strong> mesurer l'audience du site.<br>
    <strong>Base légale&nbsp;:</strong> intérêt légitime (article 6.1.f du RGPD). Conforme à
    l'exemption de consentement prévue par la CNIL pour les mesures d'audience strictement
    nécessaires au fonctionnement du site (délibération n°2020-091).<br>
    <strong>Durée de conservation&nbsp;:</strong> 13 mois.
</p>

<h3>3. Commentaires</h3>
<p>
    Si vous laissez un commentaire sur un article, nous collectons votre
    <strong>nom (ou pseudonyme)</strong>, votre <strong>adresse e-mail</strong> (facultative,
    non publiée) et le <strong>contenu</strong> du commentaire. Ces données sont conservées
    tant que l'article est en ligne.
</p>
<p>
    <strong>Finalité&nbsp;:</strong> permettre les échanges autour des articles publiés.<br>
    <strong>Base légale&nbsp;:</strong> votre consentement.
</p>

<h2>Destinataires</h2>
<p>
    Vos données ne sont <strong>ni vendues, ni louées, ni transmises à des tiers</strong>
    à des fins commerciales. Elles sont stockées sur les serveurs de notre hébergeur
    (OVH SAS, 2 rue Kellermann, 59100 Roubaix, France), exclusivement dans l'Union européenne.
</p>
<p>
    Pour l'envoi technique des e-mails de la newsletter, nous pouvons être amenés à recourir
    à un prestataire de routage (par exemple SendGrid). Dans ce cas, ce prestataire agit en
    tant que sous-traitant au sens du RGPD et ne traite vos données que pour acheminer le message.
</p>

<h2>Cookies</h2>
<p>
    Le site n'utilise <strong>aucun cookie publicitaire ni cookie de suivi tiers</strong>.
    Seuls des cookies techniques strictement nécessaires (cookie de session pour
    l'authentification administrateur) peuvent être déposés. Ces cookies sont exemptés de
    consentement par la CNIL.
</p>

<h2>Vos droits</h2>
<p>Conformément au RGPD, vous disposez des droits suivants&nbsp;:</p>
<ul>
    <li><strong>Droit d'accès</strong>&nbsp;: obtenir une copie des données vous concernant.</li>
    <li><strong>Droit de rectification</strong>&nbsp;: corriger des données inexactes.</li>
    <li><strong>Droit à l'effacement</strong>&nbsp;: demander la suppression de vos données.</li>
    <li><strong>Droit d'opposition</strong>&nbsp;: vous opposer au traitement (notamment pour
        la mesure d'audience fondée sur l'intérêt légitime).</li>
    <li><strong>Droit à la portabilité</strong>&nbsp;: récupérer vos données dans un format structuré.</li>
    <li><strong>Droit de retirer votre consentement</strong> à tout moment (lien de
        désinscription dans chaque e-mail, ou par message à
        <a href="mailto:contact@bernardpoignant.fr">contact@bernardpoignant.fr</a>).</li>
</ul>
<p>
    Pour exercer ces droits, écrivez à <a href="mailto:contact@bernardpoignant.fr">contact@bernardpoignant.fr</a>.
    Nous répondrons dans un délai maximum d'un mois.
</p>

<h2>Réclamation</h2>
<p>
    Si vous estimez que vos droits ne sont pas respectés, vous pouvez introduire une
    réclamation auprès de la <strong>Commission nationale de l'informatique et des libertés
    (CNIL)</strong>&nbsp;: <a href="https://www.cnil.fr" target="_blank" rel="noopener">www.cnil.fr</a>.
</p>
""".strip()


DEFAULT_PAGES = [
    {
        'slug': 'biographie',
        'title': 'Biographie',
        'meta_description': 'Biographie de Bernard Poignant : enseignant d\'histoire, ancien maire de Quimper, député européen et conseiller de François Hollande.',
        'content_html': BIOGRAPHIE_HTML,
    },
    {
        'slug': 'mentions-legales',
        'title': 'Mentions légales',
        'meta_description': 'Mentions légales du site bernardpoignant.fr — éditeur, hébergeur, propriété intellectuelle.',
        'content_html': MENTIONS_LEGALES_HTML,
    },
    {
        'slug': 'confidentialite',
        'title': 'Politique de confidentialité',
        'meta_description': 'Politique de confidentialité de bernardpoignant.fr — données collectées, finalités, durée de conservation, vos droits RGPD.',
        'content_html': CONFIDENTIALITE_HTML,
    },
]
