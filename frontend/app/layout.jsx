export const metadata = { title: "Vega" };
export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body style={{ margin: 0, padding: 0, background: "#0a0a0b" }}>{children}</body>
    </html>
  );
}
