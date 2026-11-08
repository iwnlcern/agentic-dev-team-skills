#[path = "../../generated/api/api_3_0.rs"]
mod api;
use api::{DecodedResponse, Value, WireType, object, text};
fn unhex(s:&str)->Result<Vec<u8>,()>{if s.len()%2!=0{return Err(());}s.as_bytes().chunks(2).map(|c|{let s=std::str::from_utf8(c).map_err(|_|())?;u8::from_str_radix(s,16).map_err(|_|())}).collect()}
fn string<'a>(v:&'a Value,k:&str)->Result<&'a str,()>{v.get(k)?.string()}
fn run(v:&Value)->Result<Value,()>{
 let valid=|b|object(vec![("valid",Value::Bool(b))]);
 match string(v,"action")? {
  "parse"=>Ok(valid(api::parse_strict_json(string(v,"text")?.as_bytes()).is_ok())),
  "schema"=>Ok(valid(api::supports_schema(string(v,"schema")?))),
  "validate"=>Ok(valid(api::validate_type(string(v,"type")?,v.get("value")?))),
  "encode"=>{let bytes=api::encode_request(string(v,"operation")?,string(v,"id")?,v.get("value")?)?;Ok(object(vec![("hex",text(&bytes.iter().map(|b|format!("{b:02x}")).collect::<String>()))]))},
  "decode"=>{
   let connect=string(v,"connect")?;let observation=if connect.is_empty(){DecodedResponse::Unavailable}else{api::decode_response(&unhex(connect)?,"protocol.connect","1",None)};
   let context=match &observation {DecodedResponse::Connect(_,ctx)=>Some(ctx),_=>None};
   let response=api::decode_response(&unhex(string(v,"frame")?)?,string(v,"operation")?,string(v,"id")?,context);
   Ok(match response {DecodedResponse::Unavailable=>object(vec![("unavailable",Value::Bool(true))]),DecodedResponse::Error(error)=>object(vec![("error",error.to_value())]),DecodedResponse::Connect(result,_)=>object(vec![("result",result.to_value())]),other=>{let all=other.result_value().ok_or(())?;if string(v,"operation")?!="protocol.schema"{return Ok(object(vec![("result",all)]));}object(vec![("result",object(vec![("protocol",all.get("protocol")?.clone()),("schema_digest",all.get("schema_digest")?.clone()),("schema_byte_count",all.get("schema_byte_count")?.clone())]))])}})
  },_=>Err(())
 }
}
fn main(){let path=std::env::args().nth(1).expect("scenario path");let data=std::fs::read_to_string(path).expect("scenario file");for line in data.lines(){let scenario=api::parse_strict_json(line.as_bytes()).expect("scenario JSON");let result=run(&scenario).expect("scenario result");println!("{}",api::canonical(&result));}}
