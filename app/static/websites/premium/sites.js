(() => {
  const menu = document.querySelector('.menu-toggle');
  const nav = document.getElementById('main-nav');
  const dialog = document.getElementById('signup');
  const form = document.getElementById('signup-form');
  const email = document.getElementById('reader-email');
  const result = document.getElementById('signup-result');
  let lastTrigger;
  menu.addEventListener('click', () => {
    const open = menu.getAttribute('aria-expanded') !== 'true';
    menu.setAttribute('aria-expanded', String(open));
    nav.classList.toggle('is-open', open);
  });
  function openSignup(trigger) {
    if (dialog.open) return;
    lastTrigger = trigger || document.activeElement;
    form.hidden = false;
    result.hidden = true;
    email.value = '';
    dialog.showModal();
    email.focus({preventScroll:true});
  }
  function closeSignup() { if (dialog.open) dialog.close(); }
  document.querySelectorAll('[data-signup]').forEach(button => button.addEventListener('click', () => openSignup(button)));
  dialog.querySelectorAll('[data-close],.dialog-close').forEach(button => button.addEventListener('click', closeSignup));
  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) closeSignup();
  });
  dialog.addEventListener('close', () => {
    email.value = '';
    if (lastTrigger && lastTrigger.isConnected && lastTrigger.focus) lastTrigger.focus({preventScroll:true});
  });
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    email.value = '';
    form.hidden = true;
    result.hidden = false;
    result.querySelector('button').focus();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && nav.classList.contains('is-open')) {
      menu.setAttribute('aria-expanded', 'false');
      nav.classList.remove('is-open');
      menu.focus();
    }
  });
  window.addEventListener('message', event => {
    if (event.source !== parent || event.data?.type !== 'premium:signup') return;
    openSignup();
  });
})();
