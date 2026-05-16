// Solar Linear icons from SVGRepo, normalized to currentColor for the local UI theme.
const ICONS={
  home:'<path d="M22 22L2 22" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M2 11L10.1259 4.49931C11.2216 3.62279 12.7784 3.62279 13.8741 4.49931L22 11" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M15.5 5.5V3.5C15.5 3.22386 15.7239 3 16 3H18.5C18.7761 3 19 3.22386 19 3.5V8.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M4 22V9.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M20 22V9.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M15 22V17C15 15.5858 15 14.8787 14.5607 14.4393C14.1213 14 13.4142 14 12 14C10.5858 14 9.87868 14 9.43934 14.4393C9 14.8787 9 15.5858 9 17V22" stroke="currentColor" stroke-width="1.5"/><path d="M14 9.5C14 10.6046 13.1046 11.5 12 11.5C10.8954 11.5 10 10.6046 10 9.5C10 8.39543 10.8954 7.5 12 7.5C13.1046 7.5 14 8.39543 14 9.5Z" stroke="currentColor" stroke-width="1.5"/>',
  playCircle:'<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="1.5"/><path d="M15.4137 10.941C16.1954 11.4026 16.1954 12.5974 15.4137 13.059L10.6935 15.8458C9.93371 16.2944 9 15.7105 9 14.7868L9 9.21316C9 8.28947 9.93371 7.70561 10.6935 8.15419L15.4137 10.941Z" stroke="currentColor" stroke-width="1.5"/>',
  presentationGraph:'<path d="M2 2H22" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M9 10.5L10.2929 9.20711C10.6262 8.87377 10.7929 8.70711 11 8.70711C11.2071 8.70711 11.3738 8.87377 11.7071 9.20711L12.2929 9.79289C12.6262 10.1262 12.7929 10.2929 13 10.2929C13.2071 10.2929 13.3738 10.1262 13.7071 9.79289L15 8.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M12 21L12 17" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M10 22L12 21" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M14 22L12 21" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M20 2V10.5C20 13.5641 20 15.0962 18.9958 16.0481C17.9916 17 16.3753 17 13.1429 17H10.8571C7.62465 17 6.00841 17 5.00421 16.0481C4 15.0962 4 13.5641 4 10.5V2" stroke="currentColor" stroke-width="1.5"/>',
  tuning:'<path d="M2 12C2 7.28595 2 4.92893 3.46447 3.46447C4.92893 2 7.28595 2 12 2C16.714 2 19.0711 2 20.5355 3.46447C22 4.92893 22 7.28595 22 12C22 16.714 22 19.0711 20.5355 20.5355C19.0711 22 16.714 22 12 22C7.28595 22 4.92893 22 3.46447 20.5355C2 19.0711 2 16.714 2 12Z" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="10" r="2" stroke="currentColor" stroke-width="1.5"/><circle cx="2" cy="2" r="2" transform="matrix(1 0 0 -1 14 16)" stroke="currentColor" stroke-width="1.5"/><path d="M8 14V19" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M16 10V5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M8 5V6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M16 19V18" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
  userPlus:'<circle cx="12" cy="6" r="4" stroke="currentColor" stroke-width="1.5"/><path d="M15 13.3271C14.0736 13.1162 13.0609 13 12 13C7.58172 13 4 15.0147 4 17.5C4 19.9853 4 22 12 22C17.6874 22 19.3315 20.9817 19.8068 19.5" stroke="currentColor" stroke-width="1.5"/><circle cx="18" cy="16" r="4" stroke="currentColor" stroke-width="1.5"/><path d="M18 14.6667V17.3333" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M16.6665 16L19.3332 16" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>',
  exit:'<path d="M9 4.5H8C5.64298 4.5 4.46447 4.5 3.73223 5.23223C3 5.96447 3 7.14298 3 9.5V14.5C3 16.857 3 18.0355 3.73223 18.7678C4.46447 19.5 5.64298 19.5 8 19.5H9" stroke="currentColor" stroke-width="1.5"/><path d="M9 6.4764C9 4.18259 9 3.03569 9.70725 2.4087C10.4145 1.78171 11.4955 1.97026 13.6576 2.34736L15.9864 2.75354C18.3809 3.17118 19.5781 3.37999 20.2891 4.25826C21 5.13652 21 6.40672 21 8.94711V15.0529C21 17.5933 21 18.8635 20.2891 19.7417C19.5781 20.62 18.3809 20.8288 15.9864 21.2465L13.6576 21.6526C11.4955 22.0297 10.4145 22.2183 9.70725 21.5913C9 20.9643 9 19.8174 9 17.5236V6.4764Z" stroke="currentColor" stroke-width="1.5"/><path d="M12 11V13" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
  restart:'<path d="M18.364 8.05026L17.6569 7.34315C14.5327 4.21896 9.46734 4.21896 6.34315 7.34315C3.21895 10.4673 3.21895 15.5327 6.34315 18.6569C9.46734 21.7811 14.5327 21.7811 17.6569 18.6569C19.4737 16.84 20.234 14.3668 19.9377 12.0005M18.364 8.05026H14.1213M18.364 8.05026V3.80762" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>',
  magnifer:'<circle cx="11.5" cy="11.5" r="9.5" stroke="currentColor" stroke-width="1.5"/><path d="M18.5 18.5L22 22" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
  checkSquare:'<path d="M2 12C2 7.28595 2 4.92893 3.46447 3.46447C4.92893 2 7.28595 2 12 2C16.714 2 19.0711 2 20.5355 3.46447C22 4.92893 22 7.28595 22 12C22 16.714 22 19.0711 20.5355 20.5355C19.0711 22 16.714 22 12 22C7.28595 22 4.92893 22 3.46447 20.5355C2 19.0711 2 16.714 2 12Z" stroke="currentColor" stroke-width="1.5"/><path d="M8.5 12.5L10.5 14.5L15.5 9.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>',
  trash:'<path d="M20.5001 6H3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M18.8332 8.5L18.3732 15.3991C18.1962 18.054 18.1077 19.3815 17.2427 20.1907C16.3777 21 15.0473 21 12.3865 21H11.6132C8.95235 21 7.62195 21 6.75694 20.1907C5.89194 19.3815 5.80344 18.054 5.62644 15.3991L5.1665 8.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M9.1709 4C9.58273 2.83481 10.694 2 12.0002 2C13.3064 2 14.4177 2.83481 14.8295 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
  downloadSquare:'<path d="M12 7L12 14M12 14L15 11M12 14L9 11" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M16 17H12H8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M2 12C2 7.28595 2 4.92893 3.46447 3.46447C4.92893 2 7.28595 2 12 2C16.714 2 19.0711 2 20.5355 3.46447C22 4.92893 22 7.28595 22 12C22 16.714 22 19.0711 20.5355 20.5355C19.0711 22 16.714 22 12 22C7.28595 22 4.92893 22 3.46447 20.5355C2 19.0711 2 16.714 2 12Z" stroke="currentColor" stroke-width="1.5"/>'
};

export const SOLAR_ICON_SOURCE='SVGRepo Solar Linear Icons';

export function solarIcon(name,className=''){
  const body=ICONS[name]||ICONS.home;
  const cls=className?` class="${className}"`:'';
  return `<svg${cls} data-solar-icon="${name}" aria-hidden="true" viewBox="0 0 24 24" fill="none">${body}</svg>`;
}

function replaceSvg(root,selector,name,className){
  const el=root.querySelector(selector);
  if(el)el.outerHTML=solarIcon(name,className);
}

export function applySolarStaticIcons(root=document){
  replaceSvg(root,'#nav button[data-view="accounts"] > svg','home','nav-icon');
  const groups=root.querySelectorAll('#nav .nav-group-head .nav-group-icon svg');
  if(groups[0])groups[0].outerHTML=solarIcon('playCircle','nav-icon');
  if(groups[1])groups[1].outerHTML=solarIcon('presentationGraph','nav-icon');
  if(groups[2])groups[2].outerHTML=solarIcon('tuning','nav-icon');
  replaceSvg(root,'#close-all-roblox-btn svg','exit','btn-icon');
  replaceSvg(root,'#add-btn svg','userPlus','btn-icon');
  replaceSvg(root,'#reload-cookies-btn svg','restart','btn-icon');
  replaceSvg(root,'.search-wrap svg','magnifer','search-icon');
  root.querySelectorAll('.reset-action svg').forEach(el=>{el.outerHTML=solarIcon('restart','btn-icon')});
  root.querySelectorAll('.save-action svg').forEach(el=>{el.outerHTML=solarIcon('checkSquare','btn-icon')});
}
