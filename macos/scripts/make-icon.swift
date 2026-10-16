// Draws the app icon into an .iconset folder: swift scripts/make-icon.swift <out.iconset>
import AppKit

let out = CommandLine.arguments[1]
try FileManager.default.createDirectory(atPath: out, withIntermediateDirectories: true)

func png(_ px: Int) -> Data {
    let s = CGFloat(px)
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

    // body on the macOS icon grid (824 of 1024), with a soft shadow
    let inset = s * 100 / 1024
    let body = NSRect(x: inset, y: inset * 1.1, width: s - 2 * inset, height: s - 2 * inset)
    let shape = NSBezierPath(roundedRect: body, xRadius: body.width * 0.225, yRadius: body.width * 0.225)
    NSGraphicsContext.saveGraphicsState()
    let shadow = NSShadow()
    shadow.shadowBlurRadius = s * 0.03
    shadow.shadowOffset = NSSize(width: 0, height: -s * 0.012)
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.35)
    shadow.set()
    NSColor.black.setFill()
    shape.fill()
    NSGraphicsContext.restoreGraphicsState()
    NSGradient(colors: [NSColor(srgbRed: 0.20, green: 0.22, blue: 0.36, alpha: 1),
                        NSColor(srgbRed: 0.07, green: 0.08, blue: 0.15, alpha: 1)])!.draw(in: shape, angle: -90)

    // the stack of workstreams
    let config = NSImage.SymbolConfiguration(pointSize: s * 0.44, weight: .semibold)
        .applying(NSImage.SymbolConfiguration(paletteColors: [NSColor(white: 0.96, alpha: 1)]))
    let stack = NSImage(systemSymbolName: "square.stack.3d.up.fill", accessibilityDescription: nil)!
        .withSymbolConfiguration(config)!
    let size = stack.size
    stack.draw(in: NSRect(x: (s - size.width) / 2, y: (s - size.height) / 2 - s * 0.01,
                          width: size.width, height: size.height))

    // the "your turn" dot
    let d = s * 0.14
    let dot = NSRect(x: body.maxX - d * 1.35, y: body.maxY - d * 1.35, width: d, height: d)
    NSColor(srgbRed: 0.07, green: 0.08, blue: 0.15, alpha: 1).setFill()
    NSBezierPath(ovalIn: dot.insetBy(dx: -s * 0.018, dy: -s * 0.018)).fill()
    NSColor(srgbRed: 0.25, green: 0.85, blue: 0.85, alpha: 1).setFill()
    NSBezierPath(ovalIn: dot).fill()

    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for (name, px) in [("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64), ("128x128", 128),
                   ("128x128@2x", 256), ("256x256", 256), ("256x256@2x", 512), ("512x512", 512),
                   ("512x512@2x", 1024)] {
    try png(px).write(to: URL(fileURLWithPath: "\(out)/icon_\(name).png"))
}
