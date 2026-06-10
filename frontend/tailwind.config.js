/** @type {import('tailwindcss').Config} */
// Tailwind v3 + DaisyUI 3 build. We intentionally stay on these majors:
// DaisyUI >=4 and Tailwind >=4 emit oklch()/@property/color-mix() CSS that
// the closed-network browsers (pre-Chromium 111) cannot parse.
module.exports = {
	content: ["./templates/**/*.html"],
	plugins: [require("daisyui")],
	daisyui: {
		logs: false,
		// DaisyUI 3 has no built-in "nord" theme — recreate it so the
		// data-theme="nord" attribute used across templates keeps working.
		themes: [
			{
				nord: {
					primary: "#5E81AC",
					secondary: "#81A1C1",
					accent: "#88C0D0",
					neutral: "#4C566A",
					"base-100": "#ECEFF4",
					"base-200": "#E5E9F0",
					"base-300": "#D8DEE9",
					"base-content": "#2E3440",
					info: "#B48EAD",
					success: "#A3BE8C",
					warning: "#EBCB8B",
					error: "#BF616A",
				},
			},
			"night",
		],
		darkTheme: "night",
	},
};
