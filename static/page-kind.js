// Runs before the stylesheet applies so the right page is shown without flicker.
// Kept in a file (not inline) so the Content-Security-Policy can forbid inline scripts.
document.documentElement.dataset.page = location.pathname === "/configuracao" ? "configuration" : location.pathname === "/monitoramento" ? "monitoring" : location.pathname === "/sistema" ? "system" : location.pathname === "/cli" ? "cli" : "dashboard";
