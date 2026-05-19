from PIL import Image
import numpy as np

img = Image.open("jigsaw.webp").convert("RGB")
w, h = img.size

tile_w = w // 5
tile_h = h // 5

mapping = [
    (0,0, 2,1), (0,1, 1,1), (0,2, 4,1), (0,3, 0,3), (0,4, 0,1),
    (1,0, 1,4), (1,1, 2,0), (1,2, 2,4), (1,3, 4,2), (1,4, 2,2),
    (2,0, 0,0), (2,1, 3,2), (2,2, 4,3), (2,3, 3,0), (2,4, 3,4),
    (3,0, 1,0), (3,1, 2,3), (3,2, 3,3), (3,3, 4,4), (3,4, 0,2),
    (4,0, 3,1), (4,1, 1,2), (4,2, 1,3), (4,3, 0,4), (4,4, 4,0),
]

out = Image.new("RGB", (tile_w * 5, tile_h * 5))

for sr, sc, or_, oc in mapping:
    left   = sc * tile_w
    top    = sr * tile_h
    tile   = img.crop((left, top, left + tile_w, top + tile_h))
    out.paste(tile, (oc * tile_w, or_ * tile_h))

arr = np.array(out, dtype=np.float64)

# Method 1: floor (truncation)
gray1 = (arr[:,:,0]*0.2126 + arr[:,:,1]*0.7152 + arr[:,:,2]*0.0722).astype(np.uint8)
Image.fromarray(gray1, mode="L").save("grayscale_floor.png")

# Method 2: round half up
gray2 = np.floor(arr[:,:,0]*0.2126 + arr[:,:,1]*0.7152 + arr[:,:,2]*0.0722 + 0.5).astype(np.uint8)
Image.fromarray(gray2, mode="L").save("grayscale_round.png")

# Method 3: PIL's built-in convert L (uses ITU-R 601: 0.299R + 0.587G + 0.114B)
out.convert("L").save("grayscale_pil.png")

# Method 4: use uint8 arithmetic directly (no float)
arr_u8 = np.array(out, dtype=np.uint8)
# dot product in float32
gray4 = (arr_u8[:,:,0].astype(np.float32)*0.2126 +
         arr_u8[:,:,1].astype(np.float32)*0.7152 +
         arr_u8[:,:,2].astype(np.float32)*0.0722).astype(np.uint8)
Image.fromarray(gray4, mode="L").save("grayscale_float32.png")

# Method 5: numpy clip + round with float32
gray5 = np.clip(
    arr_u8[:,:,0].astype(np.float32)*0.2126 +
    arr_u8[:,:,1].astype(np.float32)*0.7152 +
    arr_u8[:,:,2].astype(np.float32)*0.0722,
    0, 255
)
Image.fromarray(np.round(gray5).astype(np.uint8), mode="L").save("grayscale_float32_round.png")

print("Generated 5 variants:")
print("  grayscale_floor.png       - truncation (float64)")
print("  grayscale_round.png       - round half up (float64)")
print("  grayscale_pil.png         - PIL built-in L (ITU-R 601)")
print("  grayscale_float32.png     - truncation (float32)")
print("  grayscale_float32_round.png - round (float32)")
print()
print(f"Image size: {out.size}, tile size: {tile_w}x{tile_h}")

# Show sample pixel values for comparison
print("\nSample pixel [0,0] RGB:", arr_u8[0,0])
print("  floor64:", gray1[0,0])
print("  round64:", gray2[0,0])
print("  pil:    ", np.array(out.convert('L'))[0,0])
print("  trunc32:", gray4[0,0])
print("  round32:", np.round(gray5).astype(np.uint8)[0,0])