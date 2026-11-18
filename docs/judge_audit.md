# Judge audit

Chạy: `python -m lab9.judge --csv <file> --audit 5`

| Tiêu chí | n | κ | Precision | Recall | Đạt FLOORS (κ≥0.6, P≥0.8, R≥0.7)? | Quyết định |
|---|---:|---:|---:|---:|---|---|
| tone_ok | 100 | 0.757 | 0.857 | 0.800 | Có | Dùng có giám sát cho tone; không suy rộng sang tính đúng nghiệp vụ |
| helpful | 100 | 0.375 | 1.000 | 0.300 | Không | Loại khỏi gate; recall thấp, bỏ sót 21/30 lỗi |

## ≥5 case audit thủ công
| # | Case | Judge nói | Người nói | Ai đúng? | Hành động/rubric |
|---|---|---|---|---|---|
| 1 | tone H-008: “...anh rồi nha” | PASS | FAIL | Người | Nêu rõ xưng hô/từ khẩu ngữ không phù hợp khách hàng là FAIL; không chỉ chấm từ khóa lịch sự. |
| 2 | tone H-029: “không cần nhắn thêm” | PASS | FAIL | Người | Bổ sung tiêu chí cấm giọng ra lệnh/đóng hội thoại thiếu tôn trọng; đánh giá toàn câu. |
| 3 | tone H-042: câu ngắn, lịch sự, có số tiền và thời gian | FAIL | PASS | Người | Không bắt buộc lời xin lỗi nếu trả lời trực tiếp, đủ thông tin và lịch sự; bỏ tiêu chí “thiếu xin lỗi” máy móc. |
| 4 | tone H-051: dùng “refund” | FAIL | PASS | Người | Từ “refund” được CS LogiX cho phép; rubric không phạt tiếng Anh đơn lẻ nếu câu vẫn rõ nghĩa. |
| 5 | tone H-081: câu từ chối lịch sự, đúng chính sách | FAIL | PASS | Judge | Ghi chú CSV cho biết nhãn người đọc nhầm ticket; sửa nhãn gold trước khi dùng case để chấm/calibrate. |
| 6 | helpful HL-015: reply 55.000đ, ledger 40.000đ | PASS | FAIL | Người | Bổ sung đối chiếu con số reply với ledger làm điều kiện bắt buộc; không để văn phong dài/lịch sự bù lỗi nghiệp vụ. |
| 7 | helpful HL-020: reply 90.000đ, ledger 120.000đ | PASS | FAIL | Người | Thêm kiểm tra định lượng số tiền (và order/claim) với nguồn sự thật; thiếu đối chiếu phải abstain. |
| 8 | helpful HL-047: “Ok bạn... nhé” | PASS | FAIL | Người | Rubric helpful phải tính phù hợp xưng hô, không chỉ nội dung xử lý. |
| 9 | helpful HL-091: “Ok bạn... nhé” | PASS | FAIL | Người | Dùng thêm ví dụ xưng hô không phù hợp trong calibration; tránh dựa vào mẫu câu lịch sự chung. |
| 10 | helpful HL-100: reply 55.000đ, ledger 35.000đ | PASS | FAIL | Người | Thêm các phản ví dụ sai số tiền ngắn để ngăn judge bỏ qua lỗi khi reply súc tích. |

Ma trận lớp dương tính `FAIL`: `tone_ok` TP=24, FP=4, FN=6, TN=66; `helpful` TP=9, FP=0, FN=21, TN=70. Rogan–Gladen ước lượng tỷ lệ lỗi `tone_ok` 0.28 → 0.30 (sensitivity=recall 0.80, specificity=66/70); `helpful` 0.09 → 0.30 (sensitivity=0.30, specificity=1.00). Đây là hiệu chỉnh trên replay công khai, không phải ước lượng đại diện production.

## Kết luận
- Chỉ dùng `tone_ok` như tín hiệu hỗ trợ: đạt cả ba floors, nhưng audit cho thấy lỗi nhãn và bất đồng về phong cách; cần giữ review người và hiệu chỉnh rubric.
- Loại `helpful` khỏi gate: dù precision=1.00, recall=0.30 và κ=0.375; 21 false negative cho thấy judge thường bỏ qua lỗi hữu ích cốt lõi như sai số tiền.
- Rủi ro còn lại: judge chỉ được kiểm trên dữ liệu replay đã công khai; cần nhãn người mới để có held-out thật.
