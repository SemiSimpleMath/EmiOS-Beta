;(function() {
  const dashboard = document.querySelector('.dashboard');
  if (!dashboard) return;  
  const widgets = Array.from(dashboard.querySelectorAll('.widget'));
  let swiper = null;

  // Helper to detect mobile viewport
  const isMobile = () => window.matchMedia("(max-width: 767px)").matches;

  // Inject mobile-specific CSS once
  function addMobileStyles() {
    if (document.getElementById('mobile-swiper-styles')) return;
    const style = document.createElement('style');
    style.id = 'mobile-swiper-styles';
    style.textContent = `
      .mobile-swiper-container {
        width: 100vw;
        height: calc(100vh - var(--header-height) - var(--footer-height));
        position: relative;
        overflow: hidden;
      }
      .swiper-wrapper { display: flex; }
      .swiper-slide {
        flex-shrink: 0;
        width: 100vw;
        height: 100%;
        box-sizing: border-box;
        padding: 0.5rem;
        overflow-y: auto;
        display: flex;
        flex-direction: column;
      }
      .swiper-pagination {
        position: absolute;
        bottom: 5px;
        width: 100%;
      }
      .swiper-pagination-bullet { opacity: 0.6; }
      .swiper-pagination-bullet-active { opacity: 1; }
    `;
    document.head.appendChild(style);
  }

  // Move original widgets into Swiper and hide dashboard
  function initMobileLayout() {
    if (!isMobile() || document.querySelector('.mobile-swiper-container')) return;
    addMobileStyles();

    const container = document.createElement('div');
    container.className = 'mobile-swiper-container swiper';

    const wrapper = document.createElement('div');
    wrapper.className = 'swiper-wrapper';

    widgets.forEach(w => {
      // store original position
      if (!w._orig) {
        w._orig = { parent: w.parentNode, next: w.nextSibling };
      }
      const slide = document.createElement('div');
      slide.className = 'swiper-slide';
      slide.appendChild(w);
      wrapper.appendChild(slide);
    });

    const pagination = document.createElement('div');
    pagination.className = 'swiper-pagination';

    container.appendChild(wrapper);
    container.appendChild(pagination);
    dashboard.parentNode.insertBefore(container, dashboard.nextSibling);
    dashboard.classList.add('hidden');

    swiper = new Swiper('.mobile-swiper-container', {
      direction: 'horizontal',
      slidesPerView: 1,
      pagination: { el: '.swiper-pagination', clickable: true },
      keyboard: { enabled: true },
      a11y: true,
      on: {
        touchStart(e) {
          const t = e.target;
          if (t.tagName === 'TEXTAREA' || t.closest('form')) {
            swiper.allowTouchMove = false;
          }
        },
        touchEnd() { swiper.allowTouchMove = true; }
      }
    });

    // Prevent Swiper from hijacking chat input
    const chatInput = container.querySelector('#chat-input');
    if (chatInput) {
      ['touchstart','focus'].forEach(evt =>
        chatInput.addEventListener(evt, () => swiper.allowTouchMove = false)
      );
      chatInput.addEventListener('blur', () => setTimeout(() => swiper.allowTouchMove = true, 200));
    }
  }

  // Restore all widgets back to dashboard
  function restoreDesktopLayout() {
    if (isMobile()) return;
    const container = document.querySelector('.mobile-swiper-container');
    if (!container) return;

    // destroy swiper instance
    if (swiper) { swiper.destroy(true, true); swiper = null; }

    // put widgets back
    widgets.forEach(w => {
      const { parent, next } = w._orig || {};
      if (parent) {
        next ? parent.insertBefore(w, next) : parent.appendChild(w);
      }
      w.classList.remove('mobile-view');
    });

    container.remove();
    dashboard.classList.remove('hidden');
  }

  // Initialize on full load to ensure correct heights
  window.addEventListener('load', () => {
    if (isMobile()) initMobileLayout();
  });

  // Handle viewport changes
  let resizeId;
  window.addEventListener('resize', () => {
    clearTimeout(resizeId);
    resizeId = setTimeout(() => {
      isMobile() ? initMobileLayout() : restoreDesktopLayout();
    }, 200);
  });
})();
