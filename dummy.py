import matplotlib.pyplot as plt
import numpy as np

# Dữ liệu
classes = [
    "difficult", "gametocyte", "leukocyte", 
    "red blood cell", "ring", "schizont", "trophozoite"
]

train_counts = [312, 109, 72, 58123, 365, 133, 1108]
val_counts   = [66,  23, 15, 12455, 78,  28, 237]
test_counts  = [68,  24, 16, 12456, 79,  29, 239]

# Tính tổng số mẫu cho mỗi lớp
total_counts = [train + val + test for train, val, test in zip(train_counts, val_counts, test_counts)]

x = np.arange(len(classes))
width = 0.25

# Tạo subplot với 2 biểu đồ
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(18, 6))

# Biểu đồ cột (giữ nguyên)
bars1 = ax1.bar(x - width, train_counts, width, label='Train', color='#1f77b4')
bars2 = ax1.bar(x,         val_counts,   width, label='Validation', color='#ff7f0e')
bars3 = ax1.bar(x + width, test_counts,  width, label='Test', color='#2ca02c')

# Thêm nhãn số trên mỗi cột
def add_labels(bars, ax):
    for bar in bars:
        height = bar.get_height()
        ax.annotate(f'{height}',
                    xy=(bar.get_x() + bar.get_width() / 2, height),
                    xytext=(0, 3),  # offset theo chiều dọc
                    textcoords="offset points",
                    ha='center', va='bottom', fontsize=9)

add_labels(bars1, ax1)
add_labels(bars2, ax1)
add_labels(bars3, ax1)

# Gắn nhãn và định dạng cho biểu đồ cột
ax1.set_xlabel('Lớp', fontsize=12)
ax1.set_ylabel('Số lượng mẫu', fontsize=12)
ax1.set_title('Phân bố dữ liệu theo lớp trên các tập Train/Validation/Test', fontsize=14)
ax1.set_xticks(x)
ax1.set_xticklabels(classes, rotation=30)
ax1.legend()

# Biểu đồ tròn cho tổng phân phối
colors = ['#ff9999', '#66b3ff', '#99ff99', '#ffcc99', '#ff99cc', '#c2c2f0', '#ffb3e6']

# Tạo function để hiển thị % chỉ khi slice đủ lớn
def autopct_format(pct):
    return f'{pct:.1f}%' if pct > 2 else ''

# Tạo pie chart không có labels trực tiếp trên chart
wedges, texts, autotexts = ax2.pie(total_counts, autopct=autopct_format, 
                                   colors=colors, startangle=90, 
                                   pctdistance=0.85)

# Định dạng cho biểu đồ tròn
ax2.set_title('Phân phối tổng các lớp trong toàn bộ dữ liệu', fontsize=14)

# Tạo legend với thông tin chi tiết thay vì labels trên chart
total_samples = sum(total_counts)
legend_labels = [f'{class_name}: {count:,} ({count/total_samples*100:.1f}%)' 
                for class_name, count in zip(classes, total_counts)]
ax2.legend(wedges, legend_labels, title="Lớp (Số mẫu)", 
          loc="center left", bbox_to_anchor=(1, 0, 0.5, 1), fontsize=10)

# Cải thiện hiển thị text cho các slice nhỏ
for autotext in autotexts:
    autotext.set_color('white')
    autotext.set_fontsize(9)
    autotext.set_weight('bold')

plt.tight_layout()
plt.show()
