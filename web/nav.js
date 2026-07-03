// Injects the shared nav and footer. Include at bottom of <body>.
(function () {
  const LOGO_SVG = `<svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
    <circle cx="28" cy="28" r="22" stroke="#0bb8b2" stroke-width="4"/>
    <circle cx="28" cy="28" r="14" stroke="#0bb8b2" stroke-width="2.5" stroke-dasharray="4 3"/>
    <path d="M31 16 L23 30 H29 L25 42 L37 26 H31 Z" fill="#f59e0b"/>
    <line x1="44" y1="44" x2="54" y2="54" stroke="#0bb8b2" stroke-width="5" stroke-linecap="round"/>
  </svg>`;

  const current = location.pathname.replace(/\/$/, '') || '/index';

  function navLink(href, label) {
    const path = href.replace(/\.html$/, '');
    const active = current.endsWith(path) ? ' style="color:#111"' : '';
    return `<li><a href="${href}"${active}>${label}</a></li>`;
  }

  const nav = document.createElement('nav');
  nav.innerHTML = `
    <a class="nav-logo" href="/index.html">
      ${LOGO_SVG}
      <span class="brand">Grid Lens</span>
    </a>
    <ul class="nav-links">
      ${navLink('/index.html', 'Home')}
      ${navLink('/pricing.html', 'Pricing')}
      ${navLink('/docs.html', 'Docs')}
      <li><a href="/subscribe.html" class="btn btn-primary">Subscribe</a></li>
    </ul>`;
  document.body.insertBefore(nav, document.body.firstChild);

  const footer = document.createElement('footer');
  footer.innerHTML = `Grid Lens &copy; ${new Date().getFullYear()}
    &nbsp;·&nbsp; <a href="mailto:support@gridlens.au">support@gridlens.au</a>
    &nbsp;·&nbsp; <a href="/privacy.html">Privacy</a>`;
  document.body.appendChild(footer);
})();
