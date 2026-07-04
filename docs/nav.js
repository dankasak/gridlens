// Injects the shared nav and footer. Include at bottom of <body>.
(function () {
  const current = location.pathname.replace(/\/$/, '') || '/index';

  function navLink(href, label) {
    const path = href.replace(/\.html$/, '');
    const active = current.endsWith(path) ? ' style="color:#111"' : '';
    return `<li><a href="${href}"${active}>${label}</a></li>`;
  }

  const nav = document.createElement('nav');
  nav.innerHTML = `
    <img src="/img/grid_lens_banner.jpg" alt="Grid Lens" class="nav-banner">
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
