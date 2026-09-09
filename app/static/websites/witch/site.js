(() => {
  'use strict';

  const body = document.body;
  const menuButton = document.querySelector('.menu-button');
  const navigation = document.getElementById('navigation');
  const reader = document.getElementById('reader-dialog');
  const newsletter = document.getElementById('newsletter-dialog');
  const readerScroll = document.getElementById('reader-scroll');
  const readingProgress = document.getElementById('reading-progress');
  const form = document.getElementById('newsletter-form');
  const email = document.getElementById('newsletter-email');
  const result = document.getElementById('newsletter-result');
  const smaller = document.getElementById('font-smaller');
  const larger = document.getElementById('font-larger');
  const theme = document.getElementById('reader-theme');
  let readerSize = 20;
  let activeDialog = null;
  const returnFocus = new WeakMap();

  function closeMenu() {
    menuButton.setAttribute('aria-expanded', 'false');
    menuButton.querySelector('.menu-glyph').textContent = '+';
    navigation.classList.remove('is-open');
  }

  menuButton.addEventListener('click', () => {
    const open = menuButton.getAttribute('aria-expanded') !== 'true';
    menuButton.setAttribute('aria-expanded', String(open));
    menuButton.querySelector('.menu-glyph').textContent = open ? '−' : '+';
    navigation.classList.toggle('is-open', open);
  });
  navigation.querySelectorAll('a').forEach(link => link.addEventListener('click', closeMenu));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && menuButton.getAttribute('aria-expanded') === 'true') {
      closeMenu();
      menuButton.focus();
    }
  });

  function updateProgress() {
    const total = readerScroll.scrollHeight - readerScroll.clientHeight;
    const percentage = total > 0 ? Math.min(100, Math.max(0, readerScroll.scrollTop / total * 100)) : 100;
    readingProgress.style.width = `${percentage}%`;
  }

  function resetSignup() {
    email.value = '';
    form.hidden = false;
    result.hidden = true;
  }

  function openDialog(dialog, trigger) {
    if (activeDialog && activeDialog !== dialog) activeDialog.close();
    closeMenu();
    returnFocus.set(dialog, trigger);
    if (dialog === newsletter) resetSignup();
    dialog.showModal();
    activeDialog = dialog;
    body.classList.add('modal-open');
    if (dialog === reader) {
      readerScroll.scrollTop = 0;
      updateProgress();
      readerScroll.focus({ preventScroll: true });
    }
  }

  document.querySelectorAll('[data-reader]').forEach(button => {
    button.addEventListener('click', () => openDialog(reader, button));
  });
  document.querySelectorAll('[data-newsletter]').forEach(button => {
    button.addEventListener('click', () => openDialog(newsletter, button));
  });
  [reader, newsletter].forEach(dialog => {
    dialog.querySelectorAll('[data-close]').forEach(button => {
      button.addEventListener('click', () => dialog.close());
    });
    // Native dialogs provide keyboard focus containment and Escape dismissal.
    dialog.addEventListener('close', () => {
      if (dialog === newsletter) resetSignup();
      if (activeDialog === dialog) {
        activeDialog = null;
        body.classList.remove('modal-open');
        const trigger = returnFocus.get(dialog);
        // A mobile navigation trigger may be hidden when its menu closes.
        if (trigger && trigger.getClientRects().length) trigger.focus({ preventScroll: true });
        else menuButton.focus({ preventScroll: true });
      }
    });
    let backdropPointer = false;
    dialog.addEventListener('pointerdown', event => {
      const box = dialog.getBoundingClientRect();
      backdropPointer = event.target === dialog && (
        event.clientX < box.left || event.clientX > box.right ||
        event.clientY < box.top || event.clientY > box.bottom
      );
    });
    dialog.addEventListener('click', event => {
      if (event.target === dialog && backdropPointer) dialog.close();
      backdropPointer = false;
    });
  });

  function resizeReader(change) {
    readerSize = Math.min(28, Math.max(16, readerSize + change));
    reader.style.setProperty('--reader-size', `${readerSize}px`);
    smaller.disabled = readerSize === 16;
    larger.disabled = readerSize === 28;
    updateProgress();
  }
  smaller.addEventListener('click', () => resizeReader(-2));
  larger.addEventListener('click', () => resizeReader(2));
  theme.addEventListener('click', () => {
    const dark = reader.classList.toggle('reader-dark');
    theme.textContent = dark ? 'Light' : 'Dark';
    theme.setAttribute('aria-pressed', String(dark));
    theme.setAttribute('aria-label', dark ? 'Use light reading mode' : 'Use dark reading mode');
  });
  readerScroll.addEventListener('scroll', updateProgress, { passive: true });
  window.addEventListener('resize', () => {
    if (reader.open) updateProgress();
    if (window.innerWidth > 850) closeMenu();
  }, { passive: true });

  const tabs = Array.from(document.querySelectorAll('[data-world]'));
  function selectWorld(tab, focus = false) {
    tabs.forEach(item => {
      const selected = item === tab;
      item.setAttribute('aria-selected', String(selected));
      item.tabIndex = selected ? 0 : -1;
      document.getElementById(item.getAttribute('aria-controls')).hidden = !selected;
    });
    if (focus) tab.focus();
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectWorld(tab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== undefined) {
        event.preventDefault();
        selectWorld(tabs[next], true);
      }
    });
  });

  form.addEventListener('submit', event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    // Demonstration only: never store, log, or transmit an email address.
    email.value = '';
    form.hidden = true;
    result.hidden = false;
    result.querySelector('[data-close]').focus();
  });
})();
