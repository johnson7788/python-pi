// Gradient "A" brand mark shared by the Login hero/card and the Home sidebar.
export function BrandMark({ size = 32 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id="aioffice-mark" x1="4" y1="4" x2="44" y2="44" gradientUnits="userSpaceOnUse">
          <stop stopColor="#5b6bf5" />
          <stop offset="1" stopColor="#8b5cf6" />
        </linearGradient>
      </defs>
      <rect x="3" y="3" width="42" height="42" rx="13" fill="url(#aioffice-mark)" />
      <path
        d="M14 35 L24 13 L34 35"
        stroke="#fff"
        strokeWidth="4.4"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
      <path d="M18.4 28.5 H29.6" stroke="#fff" strokeWidth="4.4" strokeLinecap="round" />
    </svg>
  )
}
